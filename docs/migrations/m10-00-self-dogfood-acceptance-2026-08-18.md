# M10-00 Self-Dogfood Release Pilot Acceptance

Version: 1  
Date: 2026-08-18  
Status: verified local-embedded pilot

```yaml
document_id: context.m10-00-self-dogfood-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m10-00-self-dogfood-v1
affected_tasks: [M5-01, M5-05, M6-07, M8-06, M8-09, M10-00, M10-01]
next_review: M10-01
```

## Scope

M10-00 executed three dependent required leaves through the local State MCP and
unattended dispatcher. Two worker identities and an independent verifier were
bound to the plan. Compaction, Idea, interrupt and worker-loss drills were
recorded through Context Trace, Dogfood observations and Harness events.

The campaign plan binds the base commit, execution worktree and schema registry.
Candidate E0-E9 evidence was admitted before execution. Completion uses State
MCP claim, verification and completion receipts; the final projection has no
State-write or completion authority.

## Campaign Result

| Gate | Result |
|---|---|
| required leaves | `3/3` completed |
| automatable closure | `100%` |
| campaign replay | byte-equivalent pass |
| workers / independent verifier | `2 / 1` |
| forced faults | compaction, Idea, interrupt, worker loss `4/4` |
| dogfood event coverage | `100%` |
| E0-E9 | `10/10` pass; failed experiments `[]` |
| campaign verdict | `passed`; release CI `1812/1812`, `0` skipped, `211.708020 s` |
| receipt | [`m10-00-campaign-verdict-results.json`](../../experiments/evidence/m10-00-campaign-verdict-results.json), receipt SHA-256 `a17bbd307f28a41f2972f126f87b7aeeff626a018363b2c101bd9655997dfd0e`, file SHA-256 `a0b691e18e896a719982c9f4373f05be008eb4840defded695fa195cb4da11f2` |

## Provider Measurements

| Experiment | Result |
|---|---|
| redundant-history vs packet | `32,857 -> 19,633` input tokens; `40.2471%` reduction; quality `100%` |
| near CLI character boundary | `397,631` input tokens; quality `100%`; oversize rejected before provider |
| harness ablation | bare `19,608`; State `19,649`; full `19,748`; history `32,854`; quality `100%` |
| Skill overlay | 53/501,543 B -> 2/17,349 B; `96.5409%` source-byte reduction; provider input `-5.7367%`; warnings `3 -> 0` |
| real code retrieval | input `-50.0153%`; tool calls `-57.8947%`; wall time `-27.4120%`; quality `100%` |
| effective context | bundled `258,400`; admitted catalog override `950,000` runtime tokens |
| compaction | 900K upstream failure; 700K successful compact `783,628 -> 24,776`; next packet `59,172`; recovery `100%` |

The measurements support a thin default Harness. Typed State and a full packet
add input for a small self-contained task; they earn their cost when replacing
redundant history, preventing state loss or avoiding unbounded code discovery.
Skill and retrieval sections remain conditional. The selected Skill overlay is
the provider default candidate for M10-01.

## Boundaries

The pilot uses the local-embedded capability profile and a custom local
Responses proxy. It does not claim shared-strong coordination, live forge
conflict handling, a remote Temporal deployment or a second project. Those
belong to M10-01 through M10-03.

The Codex app-server caches the model catalog at startup. The persistent config
now references the 1.05M catalog and a 700K compact threshold, but the existing
desktop process must restart before its in-process catalog changes. Explicitly
configured benchmark processes verified the new effective window.

## Reproduction

```text
.venv/bin/python tools/run_codex_usage_ab_benchmark.py --root . --iterations 3 --large-iterations 3
.venv/bin/python tools/run_codex_harness_ablation.py --root . --iterations 3
.venv/bin/python tools/run_codex_skill_overlay_ablation.py --root . --iterations 3
.venv/bin/python tools/run_codex_code_task_ablation.py --root . --iterations 3
.venv/bin/python tools/run_codex_multiturn_compaction.py --root . --large-turns 2 --compact-limit 700000
.venv/bin/python tools/run_self_dogfood_campaign_closure.py
```
