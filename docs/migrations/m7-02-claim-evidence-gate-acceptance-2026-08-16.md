# M7-02 Claim-Evidence Gate Acceptance

Version: 1  
Date: 2026-08-16  
Status: verified local-embedded shadow gate

```yaml
document_id: context.m7-02-claim-evidence-gate-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m7-02-claim-evidence-v1
affected_tasks: [M7-02, M7-03, M7-04]
next_review: M7-03
```

## Scope

M7-02 binds completion, path, verification, decision and constraint claims to
M7-01 assertion provenance. Completion and verification require current code
or current State evidence. Path claims require current-code evidence that
resolves to the claimed repository scope. Decisions require current code or
current State; constraints require current State. Official evidence remains
supplemental until M7-06 provides live source resolution.

The gate validates claim fields, declared assertion IDs, repository identity,
path type, assertion digest, source revision, retrieval receipt, evaluation
time, validity and authority policy.
Memory candidates, historical reports and non-bearing assertions cannot close
work. An allowed verdict remains a candidate for State MCP validation and has
no State write or completion authority.

## Verification

| Gate | Result |
|---|---|
| strict contracts | `context.claim-evidence-gate/v1alpha1` and `context.claim-evidence-benchmark/v1alpha1` registered with current hashes |
| focused behavior | M7-01, M7-02 and schema governance `40/40` pass |
| replay | `1000/1000`; outcomes digest `0c91d55a604244a4094b3ee2b9f00511f90efec8fc6827f52ea8528c8c76e6f2` |
| negative cases | 750/750 missing evidence, non-bearing candidate and authority mismatch cases denied |
| positive cases | 250/250 current-code completion cases allowed |
| E6 veto | false allow `0`; false deny `0`; unbound/future assertion and unresolved path/revision/artifact admission `0` |
| authority | State write and completion authority `0` |
| external services | `0` |
| receipt | [`m7-02-claim-evidence-results.json`](../../experiments/evidence/m7-02-claim-evidence-results.json) replays byte-equivalent |

## Boundaries

This evidence covers local deterministic claim admission and repository-bound
current-code resolution. State mutation remains behind State MCP authorization,
revision/CAS and typed validators. Project-specific build, test, mutation,
performance and live-device obligations remain assigned to M7-03. Official
reference refresh and assertion supersedes remain assigned to M7-06.

## Reproduction

```text
.venv/bin/python tools/generate_m7_02_acceptance.py --samples 1000
.venv/bin/python -m unittest tests.test_m7_01_assertion_provenance tests.test_m7_02_claim_evidence_gate tests.test_schema_governance -q
```
