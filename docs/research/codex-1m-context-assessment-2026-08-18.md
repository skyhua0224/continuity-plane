# Codex 1M Context Assessment

Version: 1  
Date: 2026-08-18  
Status: official-source assessment / provider usage A/B measured

```yaml
document_id: context.codex-1m-context-assessment
document_revision: 1
change_type: evidence
authority_ref: evidence-bundle://official-openai/codex-gpt-5.6-context-window/2026-08-18
affected_tasks: [M5-01, M5-02, M5-03, M5-05, M10-00]
next_review: M10-00
```

## Official Capability

The official OpenAI model page for [`gpt-5.6-sol`](https://developers.openai.com/api/docs/models/gpt-5.6.md) currently states:

| Field | Value |
|---|---:|
| context window | `1,050,000` tokens |
| maximum input | `922,000` tokens |
| maximum output | `128,000` tokens |
| long-input pricing boundary | `272,000` input tokens |

The official Codex model guide lists the GPT-5.6 family as available to Codex
surfaces and shows model selection through `/model` or `codex --model`. It does
not establish that every Codex surface, account, plan or current Session exposes
the full model context window, nor does it expose a provider compaction signal
or usage trace for this repository run.

The local environment reports `codex-cli 0.147.0`, `gpt-5.6-sol`, a configured
`1,000,000` token context window and a `900,000` token auto-compaction limit.
The shell does not export `OPENAI_API_KEY`, but Codex has API-key authentication
through a local Responses proxy. `codex exec --json` exposes
`turn.completed.usage`; input, cached input, output and reasoning-output tokens
are measurable. Billing and provider compaction signals remain unavailable.

## What 1M Changes

The larger model envelope changes the upper bound of a provider request. It does
not make full-history loading the default composition strategy. The control
plane still sends a bounded Execution Packet, current evidence refs and a
continuation cursor. A 922K input request is an upper-bound probe, not a target
packet size. Requests over 922,000 input tokens must fail closed before provider
submission; context-window capacity above that value is reserved for output and
protocol overhead.

## Quantitative Experiment Matrix

### Local deterministic track

Run the same sanitized fixtures at nominal input budgets `8K`, `32K`, `128K`,
`256K`, `512K`, `768K`, `900K` and `922K`. Record bytes, estimated token proxy,
packet bytes, retrieval bytes, restore latency, canary verdict, first-action
match, stale-decision revival and duplicate-effect count. Proxy bytes remain
labelled as proxy and cannot be reported as provider token usage.

### Provider track

When a permitted Codex/OpenAI usage trace is available, run at least three
replicates for each task class and budget with fixed model snapshot, reasoning
setting, fixture, tools and verification profile. Record provider input/output
tokens, cached input tokens, billing, wall latency, compaction signal, cut point,
PreCompact/PostCompact timing and the same safety/quality gates as the local
track. The control route and bounded packet route must use the same task and
verification scope.

### Boundary and recovery track

Probe `922,000`, `922,001`, `1,050,000` and `1,050,001` token declarations.
Only the first and the model-window declaration can be admitted as capacity
metadata; the over-input cases must be rejected before a provider call. Inject
visible compaction at 25%, 50%, 75% and 90% of the admitted input budget and
verify active leaf, latest decision, constraints, return point, first action,
acknowledged-input replay and effect idempotency.

## Acceptance

The 1M capability is useful only when all of these remain true:

- current task, latest decision and constraints recover at `100%`;
- stale decision revival, premature stop and duplicate effect remain `0`;
- the first post-compaction action matches the continuation cursor;
- quality, build, test and scope gates do not regress;
- provider metrics are marked `unavailable` when no provider trace exists;
- any token or cache improvement is compared against the same task, model,
  reasoning setting and verification budget.

The expected benefit is fewer forced provider compactions for genuinely large
tasks. It is not evidence that larger prompts are cheaper, faster or more
accurate. The bounded packet route remains the default until the provider track
passes these gates.

## Measured Provider A/B

The same `gpt-5.6-sol` model, read-only sandbox, no-tool task and expected JSON
result were run three times per case. Every sample returned the correct
`M10-00` active leaf and next action.

| Case | Samples | Mean input tokens | Mean cached input | Quality |
|---|---:|---:|---:|---:|
| redundant history | 3 | `32,857` | `13,397.333` | `100%` |
| bounded packet | 3 | `19,633` | `8,277.333` | `100%` |
| near CLI character boundary | 3 | `397,631` | `3,840` | `100%` |

The bounded packet reduced input tokens by `40.2471%` relative to the redundant
history fixture with no quality loss. Relative to the near-boundary history
fixture, it used `95.0625%` fewer input tokens. All nine runs emitted a warning
that Skill descriptions were shortened to fit the Skill context budget. This
identifies repeated Skill/catalog loading as a current fixed-cost target.

The largest accepted generated prompt contained `1,026,236` characters and
used `397,631` input tokens. A `1,083,237` character prompt was rejected by
Codex at `turn/start` because the CLI input limit is `1,048,576` characters.
The configured 1M model context therefore does not imply that one CLI turn can
submit 1M input tokens. Multi-turn accumulation and live auto-compaction remain
separate experiments.

Evidence: [`m10-00-codex-usage-ab-results.json`](../../experiments/evidence/m10-00-codex-usage-ab-results.json), receipt SHA-256 `c75a097cf0c2129b7448f01ddbf350c46e16dcefcf25743e1df12e21814351f2`, file SHA-256 `9bac6c9dd84fa1f7cea14ace0312399d86a0eb72a8b169c6d9df3edd8aad2a1a`.

## Harness Ablation

The same fixture was then run with four input compositions, three samples per
arm:

| Arm | Mean input tokens | Quality | Delta vs bare |
|---|---:|---:|---:|
| bare task instruction | `19,608` | `100%` | `0%` |
| Typed State packet | `19,649` | `100%` | `+0.2091%` |
| full packet (Skill + retrieval + cursor) | `19,748` | `100%` | `+0.7140%` |
| redundant history | `32,854` | `100%` | `+67.55%` |

The current fixture does not distinguish answer quality because all four arms
are correct. It does distinguish input cost: the packet arms avoid the
redundant-history expansion, while adding every packet section to a small task
adds input. The default composition should therefore be thin and conditional;
Skill, retrieval and cursor sections should expand only when the task signals
require them. All `12/12` ablation runs emitted the Skill-description-shortened
warning, so Skill catalog loading remains a separate optimization target.

Evidence: [`m10-00-codex-harness-ablation-results.json`](../../experiments/evidence/m10-00-codex-harness-ablation-results.json), receipt SHA-256 `49dca3507d644389a9162fd4cfb90d78ee3aec22a3d118d46702df4e5cd081a6`, file SHA-256 `a80db9970ec8d0e90529a55345d1e2d3879ef78c2094d9114f83c7d20ca69fc7`.

## Effective Context And Compaction

The bundled Codex model catalog advertised `272,000` tokens for
`gpt-5.6-sol`; the runtime applied its 95% safety factor and reported an
effective `258,400` token window. The `model_context_window=1000000` setting was
therefore capped by the catalog. A local `model_catalog_json` override raised
the runtime-reported window to `950,000` tokens.

At the 950K effective window, two large turns accumulated to `788,298` last-turn
input tokens without compaction. A 900K compaction threshold later attempted a
pre-sampling compact but the custom Responses proxy disconnected five times and
the turn failed. Reducing the threshold to `700,000` produced one successful
`context_compacted` event: compaction processed `783,628` input tokens, reduced
the carried context to `24,776` tokens, and the next packet used `59,172` input
tokens. Task recovery remained correct for every completed turn.

The local configuration now pins the 1.05M model catalog and uses a `700,000`
auto-compaction threshold. A running app-server retains its startup catalog;
new processes need an explicit override until the app-server is restarted.

Evidence: [`m10-00-codex-multiturn-700k-results.json`](../../experiments/evidence/m10-00-codex-multiturn-700k-results.json), receipt SHA-256 `813fa1ad7fd8cb68bc2ab35bcfde7e78fc44fdac8108171da684a05fac4aa3b1`, file SHA-256 `f224b1c200ab5101513ed6753c0e944946052f558a4ec5b27bf1f793f49605ef`.

## Skill Overlay

The current Codex home contains `53` Skill sources totaling `501,543` bytes.
The selected overlay contains two Skill sources totaling `17,349` bytes, a
`96.5409%` source-byte reduction. The warning count changed from `3/3` in the
full home to `0/3` in the selected and empty overlays.

Provider input changed from `19,593` tokens in the full home to `18,469` with
the selected overlay and `18,324` with no user Skills. Total-input reductions
were `5.7367%` and `6.4768%`; the remaining approximately 18K tokens belong to
model instructions, tools, plugins and project context rather than selected
Skill bodies. Quality remained `100%` in all arms.

Evidence: [`m10-00-codex-skill-overlay-minimal-results.json`](../../experiments/evidence/m10-00-codex-skill-overlay-minimal-results.json), receipt SHA-256 `01078b81e5ab51c2d23a9bf21188bcfd71b04accb46ac73542018d3bb7d83707`, file SHA-256 `c3a99ea4d06124480b4ae057f4aa41800a7a3398edc6303914f66555a86b0420`.

## Real Code Task

The provider used read-only repository tools to locate the exact M10 evidence
matrix function and status. Each arm ran three times and returned the correct
answer.

| Arm | Mean input tokens | Mean tool calls | Mean wall time |
|---|---:|---:|---:|
| bare discovery | `146,987.667` | `12.667` | `25,540.576 ms` |
| State packet | `79,814.667` | `6.667` | `23,257.596 ms` |
| retrieval packet | `73,471.333` | `5.333` | `18,539.239 ms` |

Relative to bare discovery, the retrieval packet reduced input tokens by
`50.0153%`, tool calls by `57.8947%`, and wall time by `27.4120%`, with no
quality loss.

Evidence: [`m10-00-codex-code-task-results.json`](../../experiments/evidence/m10-00-codex-code-task-results.json), receipt SHA-256 `527db1d929af621a7a73804312beffd31125e138d4900580bdd6f9dd2ee07426`, file SHA-256 `76f646be6bd3ad91e204b3ad6a245e50d4dd8d8e60ca57601bb70947c219ffe9`.

## Reproduction Inputs

```text
Official model source: https://developers.openai.com/api/docs/models/gpt-5.6.md
Codex model guide: https://developers.openai.com/codex/models.md
Local CLI check: codex --version
Provider accounting contract: context.context-accounting/v1alpha1
Recovery contracts: context.execution-packet/v1alpha1,
    context.durable-continuation/v1alpha1
```
