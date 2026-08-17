# Benchmarks

[中文](benchmarks.md)

`benchmarks/reference-results.json` contains aggregate, sanitized observations
from the release candidate. It does not contain prompts, repository paths,
thread IDs, provider credentials, or private project names.

The real-work measurements compare matched tasks against an exact expected
answer. Each arm has three runs. Reported quality is the fraction of runs that
preserved every required fact and avoided every forbidden assertion.

Metrics have stable definitions:

- input reduction is `(baseline - candidate) / baseline`;
- tool-call reduction uses completed tool calls per run;
- wall time is elapsed time from dispatch to final result;
- source-byte reduction compares all applicable Skill sources with the
  compiled selected packet;
- collaboration duplication counts repeated discovery calls across workers.

## User Tokens, Window Utilization, And Consistency

A scenario-level input-token reduction describes only that scenario. Long-term
user savings are normalized by accepted Work and report input, output, cached
input, and provider-visible reasoning tokens separately:

- `billable tokens per accepted Work` is the average usage of completed Work
  that passed its verification gates under the same provider, model, task class,
  and window configuration;
- `useful context utilization` is the share of the provider's occupied window
  used by current authoritative state, current evidence, selected Skills,
  necessary tool results, and new interaction;
- `active time to compaction` runs from a successful PostCompact canary to the
  next PreCompact signal and excludes user idle time and explicit blockers;
- `accepted Work per compaction` counts Work or durable artifacts that passed
  completion gates between visible compactions;
- `continuity consistency` reports independent correctness and error counts for
  the active leaf, latest decisions, constraints, first action, acknowledged
  input, effect watermark, and CAS conflicts.

Current public data supports separate conclusions for redundant history,
near-limit history, Skills, code retrieval, and collaboration. It does not yield
one universal token-saving percentage, and it does not prove that real sessions
now take longer to reach the next compaction. Provider window usage, compaction
signals, and accepted Work must come from the same trace; the result remains
`unavailable` when a host cannot export them. Any consistency-veto failure
invalidates a savings claim.

A longitudinal claim requires at least three matched real-session segments per
arm and reports the task class, window size, cache semantics, pricing snapshot,
idle-time exclusion, and complete consistency vector.

## Compaction And Recovery

| Comparison | Baseline | Continuity Plane | Result |
|---|---:|---:|---:|
| redundant history recovery | `32,857` input tokens | `19,633` | `-40.25%` |
| near-limit history recovery | `397,631` input tokens | `19,633` | `-95.06%` |
| expected-answer quality | `3/3` | `3/3` | no regression |

The 1M provider A/B used `3+3+3` matched runs. The 700K compaction reduced
`783,628` to `24,776`, the next packet was `59,172`, and recovery was `100%`.
The 900K upstream compact failed and is not reported as a capability. Window
utilization and Work completed between compactions await the M10-11 longitudinal
trace.

## Code Retrieval

| Metric | Unbounded | Bounded retrieval | Change |
|---|---:|---:|---:|
| input tokens | `146,987.67` | `73,471.33` | `-50.02%` |
| tool calls | `12.67` | `5.33` | `-57.89%` |
| wall time | `25.54 s` | `18.54 s` | `-27.41%` |
| quality | `3/3` | `3/3` | no regression |

## Skill Loading

The full Skill source was `501,543 B`; the selected compiled packet was `17,349 B`,
a `96.54%` source-byte reduction. Expected-answer quality was `3/3`. Long-term
provider-input changes require host token traces and cannot be inferred from
source bytes.

## Multi-Session Collaboration

| Metric | Uncoordinated | Continuity Plane | Change |
|---|---:|---:|---:|
| worker tool calls | `14.00` | `7.33` | `-47.62%` |
| duplicate tool calls | `11.33` | `5.00` | `-55.88%` |
| parallel wall time | `24.58 s` | `19.01 s` | `-22.65%` |

An independent verifier increased collaboration input tokens by `10.75%`. This
quality cost is reported explicitly. Whether it is worthwhile is evaluated by
task class, verification profile, and accepted Work.

## Consistency And Limitations

| Gate | Result |
|---|---:|
| compaction / idea / interrupt / worker-loss faults | `4/4` |
| E0-E9 campaign | `10/10` |
| dual-Session same-revision delivery | `1000/1000` |
| duplicate-notification suppression | `1000/1000` |
| offline catch-up | `2000/2000` |
| authority violations | `0` |

These results use one workstation, sanitized fixtures, and matched task classes.
They do not prove a cross-model or cross-device total token saving, and they do
not prove that a complete Docmost UI is deployed. A run with unavailable host
metrics, incomplete provider traces, or a failed consistency veto cannot produce
a total-savings claim.

Run the public synthetic local-state benchmark from a release checkout:

```bash
python benchmarks/run_local_state.py --iterations 1000
```

It creates a temporary local project, performs 1,000 validated SQLite reads,
and reports median, p95, success count, Python version, operating system class,
and machine architecture. It uses no network or external service.

The aggregate receipt includes a content digest and limitations. Raw prompts,
repository names, filesystem paths, thread identifiers, and provider metadata
are not part of the public benchmark artifact.
