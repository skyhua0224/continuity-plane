# M6 Retrieval and Recall Acceptance

Version: 1  
Date: 2026-08-15  
Status: verified local-embedded shadow adapters

```yaml
document_id: context.m6-retrieval-recall-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/c0136c17f10c93d2d308226a72df1f66a3544a6bb4f88dc7fdcc2816fe1f1aa5
affected_tasks: [M6-01, M6-02, M6-03, M6-04, M6-05, M6-06, M6-07, M7-01]
next_review: M7-02
```

## Scope

M6 provides deterministic local contracts for bounded retrieval, graph-clue
verification, candidate recall, memory ablation, asynchronous review and MCP
server admission. Retrieval, CodeGraph, memory providers, reviewers and MCP
registry adapters have no State write authority. The default topology remains
local-embedded and the acceptance run uses no external service.

## M6-01 Bounded Retrieval Route

The router classifies exact text, large-corpus text, symbol definition,
cross-repository impact and official-reference questions. It selects the
minimum admitted subset of `rg`, Zoekt, LSP, SCIP and RTFM, then allocates
query, scan and return-byte budgets to each step. Missing required verifiers,
budget overruns and stale indexes fail closed.

The fixed benchmark completed `1000/1000` replay iterations. Each iteration
executes the same five admitted question families, producing `5000/5000`
route decisions and a persisted outcome digest. Route accuracy, precision,
recall and freshness pass rate were all `1.0`; the committed local benchmark
p95 was `4.54415 ms`. These iterations measure deterministic route replay and
fault rejection; they are not 1,000 independent user questions.

## M6-02 CodeGraph Verification

CodeGraph output remains a clue. Every admitted qualified-symbol relation
requires one current `rg` evidence item and one LSP evidence item. Missing
verifiers, duplicate clues and same-name pollution are rejected. Repository
identity is bound to the trusted root; Python module prefixes are bound to the
source and LSP target paths. Resealed fake repository and module identities
fail closed. The committed receipt contains `1/1` verified clue and `2/2`
independent verifier records.

## M6-03 Recall Provider SPI

The SPI accepts bounded provider records and returns candidate-only receipts.
Current and stale candidates remain explicitly distinguished; neither receives
active-state authority. The `503` fixture returns a degraded empty result and
does not change task, decision or State. Mem0, Hindsight and Graphiti remain
replaceable external candidates; active external Recall Providers remain `0`.

## M6-04 Memory Ablation

The paired fixed conformance fixture compares no-memory execution with the
same cases and budget using the local reference candidate provider. Across `1000` cases,
baseline accuracy was `0.6`, candidate accuracy was `0.9`, absolute gain was
`0.3`, relative gain was `50%`, and paired p-value was
`9.82e-91`. Stale-decision revival, authority violations and
provider-503 State failures were all `0`. The receipt artifact binds `1001`
Recall receipts and compresses `1,092,446` bytes to `199,004` gzip bytes. The
provider admission status remains `reference_fixture_conformance_only`; this
result does not establish a gain for any external memory implementation.

## M6-05 Reviewer Adapter

Local findings are candidate-only. External submissions return an operation
reference; pending and timeout paths degrade asynchronously without blocking
the active task. Reviewer State write authority and completion authority are
both `0`. The committed acceptance paths are local, deferred and timeout
fixtures; external reviewer calls are `0`.

## M6-06 MCP Admission

The snapshot uses a local `.invalid` registry fixture with fixed revision,
content digest, publisher, license, authentication and tool scopes. Registry
content identity excludes retrieval time while each decision binds the full
request and snapshot envelope. An `official` registry additionally requires an
external trusted anchor for kind, URL, revision and digest. Write and external
effect tools require an exact trusted `project_id -> State MCP route` binding.
Admission decisions invoke no tool. Tests ran `1000` unauthorized requests
with `0` state-write or external-effect tool activations. The admission adapter
itself receives no State authority. This fixture does not claim an Official MCP
Registry snapshot.

## M6-07 Receipt, Freshness and Read Reduction

Retrieval receipts bind query digest, plan digest, revision, content hash,
byte range, execution metrics and index age. The fixed E5 corpus reduced
duplicate read bytes from `6,720,000` to `3,360,000`, a `50%` reduction.
Cache hits must resolve to an existing prior miss receipt with matching digest,
plan, query, evidence and time lineage. Retrieval provenance coverage and
bearing-assertion acceptance were `100%`. The measurement is byte accounting
for the committed fixture and is not reported as provider token reduction.

## M7-01 Provenance Dependency

Bearing assertions accept current code, current State, industry-standard, OS
official or software-official evidence. Memory candidates and historical
reports cannot support bearing assertions. Version, content hash, validity,
retrieval receipt reference, expiry and record digest are required; committed
provenance coverage is `1.0`. Current-code evidence resolves against a real
repository path and Git object or content-bound worktree revision; current
State resolves by exact source/revision; the retrieval receipt resolves by
content digest and contract validation. Live resolution of official authority
remains assigned to M7-06.

## Evidence

| Gate | Result |
|---|---|
| strict contracts | 10 Draft 2020-12 schemas registered; runtime and schema instances pass |
| committed receipts | 13 independently validated JSON documents |
| focused behavior | M6 and M7-01 focused tests pass |
| E5 quality | route/precision/recall/freshness/provenance `1.0` |
| duplicate reads | `50%` byte reduction on the fixed corpus |
| recall safety | stale revival, authority and 503 State failures `0` |
| MCP safety | unauthorized write/effect activation `0/1000` |
| external services | `0` |

Committed evidence is stored under [`experiments/retrieval`](../../experiments/retrieval).

## Reproduction

```text
.venv/bin/python tools/generate_m6_acceptance_fixtures.py --samples 1000
.venv/bin/python -m unittest tests.test_m6_01_retrieval_routing tests.test_m6_01_retrieval_benchmark tests.test_m6_02_codegraph_verification tests.test_m6_03_recall_provider tests.test_m6_04_memory_ablation tests.test_m6_05_reviewer_adapter tests.test_m6_06_mcp_admission tests.test_m6_contract_schemas tests.test_m6_acceptance_fixtures tests.test_m7_01_assertion_provenance -q
```
