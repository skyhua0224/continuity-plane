# M9-02 Project Graph Projection Acceptance

Version: 1  
Date: 2026-08-17  
Status: verified read-only projection contract

```yaml
document_id: context.m9-02-project-graph-projection-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m9-02-project-graph-projection-v1
affected_tasks: [M3-05, M8-02, M9-01, M9-02, M9-03, M9-04, M9-07]
next_review: M9-03
```

## Scope

M9-02 consumes one authenticated M9-01 State projection and emits a read-only
Project Graph, same-revision active Work set, Work Ledger and health projection.
The graph preserves parent, dependency, supersedes, experiment return point and
promotion target relationships. Active Work records preserve owner, Claim,
lease expiry, v6 fence and opaque Work provenance fields.

Legacy State can expose graph cycles and orphaned Work as health findings.
Declared open-Work overlap candidate pairs expose their overlapping scopes.
Typed State rejects active Claim ownership conflicts before M9-01 signs a
source; `claim_ownership_overlaps` is therefore a strict empty invariant in the
projection contract. Expired active leases remain visible at observation time
without granting current ownership.

## Contracts

| Contract | Responsibility |
|---|---|
| `context.project-graph-projection/v1alpha1` | signed, same-revision graph, active Work set, Work Ledger, health and zero-authority projection |
| `context.project-graph-projection-benchmark/v1alpha1` | completeness, health, bounded-work, tamper, authority and worst-path latency receipt |

## Verification

| Gate | Result |
|---|---|
| M9-02 behavior, benchmark, runner and schema tests | `21/21` pass |
| typed-state RFC3339 admission and M9-02 combined focused set | `35/35` pass |
| same revision and State digest binding | `1000/1000`; rate `1.0` |
| active Work completeness | `1000/1000`; rate `1.0` |
| cycle and orphan visibility | `1000/1000` each; rate `1.0` |
| expired branch and active-lease visibility | `1000/1000` each; rate `1.0` |
| declared Work-scope overlap visibility | `1000/1000`; rate `1.0` |
| bounded 223 x 223 scope comparison path | `1000/1000`; rate `1.0` |
| 128-Work graph completeness | `1000/1000`; rate `1.0` |
| active Claim ownership conflict admission | one signed-source fixture rejected; boolean `true` |
| coordinated projection tamper rejection | `1000/1000`; rate `1.0` |
| authority violations / provider invocations / external services | `0 / 0 / 0` |
| worst-path projection latency | p50 `4.369527 ms`; p95 `6.319825 ms`; max `8.320444 ms` |
| benchmark verdict | `passed`; failed gates `[]` |
| independent review | High `0`; Medium `0`; Low `0` |
| receipt | [`m9-02-project-graph-projection-results.json`](../../experiments/evidence/m9-02-project-graph-projection-results.json), receipt SHA-256 `cb1d0b586db09e561ea62b3f954bec2b907efaa81cad357c5916bdb237502b1c`, file SHA-256 `9eae133f5b1ded7c3f5fc1fd9a36ff893bc69418406c0a3680fea0ccfb91b478` |

The latency sample for each iteration is the maximum of the baseline active
Work path, the 223-scope declared-overlap stress path and the 128-Work graph
path. The p95 gate therefore cannot be satisfied by fast paths masking the
slowest measured path. The acceptance validator and schema both reject failed
gate receipts.

## Capacity And Integrity Boundaries

The projection rejects source data before graph construction when it exceeds
10,000 Works or items in a single projected list, 50,000 graph edges, 50,000
declared overlap references, 50,000 aggregate projected nested items, 50,000
declared scope comparisons or 50,000 health findings. Identifier and text
lengths are checked against the strict output schema. These bounds apply after
M9-01 typed-state, digest and signature validation and before copying or sorting
large nested values.

Projection validation rebuilds graph, active Work, ledger and health fields
from the signed source, then compares canonical bytes. Recomputing projection
digests and HMAC signatures after coordinated field tampering does not bypass
the source-bound rebuild. Runtime timestamp admission and all current typed
State schemas use RFC3339 timestamps with the `T` separator and timezone.

## Authority Boundaries

The projection exposes no State commit, Claim, approval, promotion or effect
tool. State write and controlled-action authority remain false; provider and
external-effect authority remain zero. Work-scope overlap findings are declared
deduplication candidates. They do not prove active Claim ownership. Claim
ownership remains an upstream Typed State invariant enforced before signing.

This acceptance does not prove a Docmost UI, browser rendering, HTTP/SSE
transport, interactive graph layout or human controlled-action flow. M9-03
through M9-07 retain their own completion gates.

## Reproduction

```text
.venv/bin/python tools/run_project_graph_projection_benchmark.py --root . --iterations 1000 --generated-at 2026-08-17T20:00:00+08:00
.venv/bin/python -m unittest tests.test_m9_02_project_graph_projection tests.test_m9_02_project_graph_benchmark tests.test_m9_02_contract_schemas -v
ruff check context_control_plane/project_graph_projection.py context_control_plane/project_graph_projection_benchmark.py tests/test_m9_02_project_graph_projection.py tests/test_m9_02_project_graph_benchmark.py tests/test_m9_02_contract_schemas.py tools/run_project_graph_projection_benchmark.py
```
