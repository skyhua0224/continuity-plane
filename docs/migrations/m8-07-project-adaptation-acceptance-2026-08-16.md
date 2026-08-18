# M8-07 ProjectAdaptation Acceptance

Version: 1  
Date: 2026-08-16  
Status: verified local-embedded shadow adapter

```yaml
document_id: context.m8-07-project-adaptation-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m8-07-project-adaptation-v1
affected_tasks: [M5-07, M6-07, M7-04, M8-07, M10-00]
next_review: M8-08
```

## Scope

M8-07 defines a provider-neutral ProjectAdaptation loop for observations that
were already verified at a State revision. A proposal may change bounded
retrieval order, common path and command references, verification hints,
Skill applicability and presentation preferences. It cannot change active
Work, claims, ownership, authorization, validators, evidence gates or effect
permissions. Candidate history is retained when an adaptation is rejected,
rolled back, opted out or reset.

The implementation is an in-memory local reference contract. It performs no
provider invocation and no external service call. Shared State MCP persistence,
multi-writer CAS and production approval transport remain deployment adapters.

## Contracts

| Contract | Responsibility |
|---|---|
| `context.project-adaptation-observation/v1alpha1` | verified run observation, measured read/verification metrics, correction and failure-fixture provenance |
| `context.project-adaptation-proposal/v1alpha1` | immutable content digest, scope, applicability, bounded changes, veto snapshot and lifecycle status |
| `context.project-adaptation-replay/v1alpha1` | three-or-more shadow projections bound to fixture, provider label and budget; non-determinism quarantine |
| `context.project-adaptation-transition/v1alpha1` | approval-bound activation and rollback receipt with revision and veto snapshot |
| `context.project-adaptation-benchmark/v1alpha1` | replay, authorization, rollback, reset, authority and latency acceptance receipt |

## Verification

| Gate | Result |
|---|---|
| focused behavior and contract tests | `11/11` pass |
| deterministic shadow replay | `2000/2000`; rate `1.0` |
| unapproved activation rejection | `1000/1000`; rate `1.0` |
| successful activation after approval | `2000/2000`; rate `1.0` |
| rollback veto recovery | `1000/1000`; rate `1.0` |
| opt-out/reset | `1000/1000`; rate `1.0` |
| authority mutation | `0` |
| provider invocations / external services | `0 / 0` |
| local benchmark latency | p50 `0.796221 ms`; p95 `1.421193 ms`; max `2.811741 ms` |
| receipt | [`m8-07-project-adaptation-results.json`](../../experiments/evidence/m8-07-project-adaptation-results.json), receipt SHA-256 `7fb769fb43df3b75e1ee177975deed12e4b4ce0ede9755c327f623b3fc83746c`, file SHA-256 `2e2947984647352405f570181a294a92c5d19b58de625eded41672a38974a1a9` |

The benchmark uses `1000` independent in-memory projects and a fixed timestamp.
The replay budget is bounded at `100` input tokens, `50` output tokens and `2`
tool calls per shadow run. The benchmark is evidence for lifecycle safety and
deterministic local behavior; it is not evidence of provider quality,
cross-process durability, or production self-modification.

## Safety boundaries

Activation requires a matching immutable proposal digest, a completed
three-run deterministic shadow replay, an approval reference and all E1/E2/E4/
E6/E8/E9 veto results. Non-deterministic replay is quarantined. Rollback uses
an earlier activated proposal whose replay and veto snapshot still pass. Reset
removes the active adaptation while preserving proposal and replay history.

The proposal schema admits only named presentation preference keys and rejects
unknown keys. Adaptation state has no State write, completion or provider-native
authority. A real deployment must bind these transitions through State MCP
authorization, expected revision/CAS and validator gates.

## Reproduction

```text
.venv/bin/python tools/run_project_adaptation_benchmark.py --samples 1000 --generated-at 2026-08-16T20:00:00+08:00
.venv/bin/python -m unittest tests.test_m8_07_project_adaptation tests.test_m8_07_contract_schemas tests.test_m8_07_benchmark -q
```
