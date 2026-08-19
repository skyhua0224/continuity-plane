# M10-11 Live Continuity Foundation

Version: 1  
Date: 2026-08-20  
Status: implementation and local lifecycle verification complete; matched live gate open

```yaml
document_id: context.m10-11-live-continuity-foundation
document_revision: 1
change_type: evidence
authority_ref: verification-run://m10-11/foundation/revision-1
supersedes: null
affected_tasks: [M5-02, M5-03, M5-05, M5-08, M10-01, M10-11]
next_review: M10-11
```

## Scope

M10-11 now has strict contracts for a pre-registered study, sanitized provider
segments, a derived comparison report, open provider capability and event
adapters, interaction cursors, and checkpoint-bound recovery envelopes. The
comparison implementation derives its verdict from source-bound inputs. It does
not accept a caller-supplied improvement result.

The Codex integration uses the official `PreCompact`, `PostCompact`, and compact
`SessionStart` lifecycle events documented at
`https://learn.chatgpt.com/docs/hooks.md`. `PreCompact` creates a checkpoint
without model-visible output. `PostCompact` verifies the checkpoint and stops
continuation when the canary fails. Compact `SessionStart` injects one bounded
Recovery Envelope.

## Implemented Gates

| Gate | Result |
|---|---:|
| live continuity wire contracts | `7` strict schemas registered with exact content hashes |
| matched sample minimum | baseline/candidate `>=3` per exact match key |
| match key | provider/model/reasoning/window/threshold/cache/tool/sandbox/adapter/repository/State/Profile/task/verification |
| portability claim | requires `>=2` project profiles, provider contracts, and collaboration modes |
| packet budget | `<=12,288 bytes` |
| recovery re-read policy | Skill/document/code/memory rereads after compaction are vetoes |
| consistency vetoes | active Work, claim, revision, first action, acknowledged input replay |
| response vetoes | direct answer `<100%`, recovery narration, unrequested table |
| improvement gates | input and total tokens/accepted Work, Work/compaction, controlled context ratio `>=30%`; output tokens/Work `>=10%` |
| authority | study, segment, report, cursor, envelope, adapter all have State/completion authority `false` |

Accepted Work must carry a unique State completion receipt. A segment that is
missing provider usage, token attribution, accepted Work, a typed compaction
boundary, or an eligible question response produces `insufficient-evidence`.
Byte counts cannot substitute for provider tokens.

## Codex Lifecycle Verification

The installed local development plugin is
`continuity-plane 0.1.0-alpha.2+codex.20260819181850`. The plugin source is
versioned under `integrations/codex/continuity-plane`; the personal marketplace
cache is a generated installation target.

One real local State/CLI/hook smoke used the current self-dogfood project and a
sanitized cursor derived from a Codex rollout tail:

| Metric | Result |
|---|---:|
| Recovery Envelope bytes | `3,037` |
| checkpoint verified | `true` |
| interaction cursor present | `true` |
| confirmed current inputs | `1` |
| `no_restate` | `true` |
| response mode | `continue-silently` |
| Skill lock | `measured`; `8` stable rule IDs |
| raw response text in envelope | `false` |

The adapter reads at most a 2 MiB rollout tail for the interaction cursor,
stores only content hashes and cursor state, and never admits the transcript to
Git or Typed State. The Codex rollout metric adapter processes the provider
archive as a single stream and retains counters, event hashes, and message
counts rather than transcript text.

## Provider Measurement Correction

The M10-00 700K compaction result is a valid historical 950K-window observation,
but its generated receipt used a text search and bound `compaction_events` to an
`item.completed/error` event. Re-reading the exact source rollout through the
strict M10-11 adapter produced:

| Metric | Result |
|---|---:|
| effective context window | `950,000` provider-reported tokens |
| explicit compaction events | `1` `event_msg/context_compacted` |
| pre-compact last input | `783,628` tokens |
| compacted context | `24,776` total-only tokens |
| first post-compact input | `59,172` tokens |

The numerical result remains historical evidence. The old receipt's
`compaction_events` array is not accepted as event provenance by M10-11. The
separate M10-00 default-catalog thread reported `258,400` tokens and three
explicit compactions; it is not a 1M-window sample.

A new Codex preflight with the fixed catalog reported `950,000`, one explicit
compaction, and a first post-compact input of `59,177` tokens. `codex exec`
reported `plugins_instructions=false` even with the `plugins` and `hooks`
features enabled, so that surface did not load the personal plugin and cannot
serve as a candidate arm. This absence is recorded as an adapter capability
gap rather than a zero-value improvement.

## Security And Portability

The provider core accepts an open provider contract ID; a new provider can
register an external adapter without editing a provider enum. Unknown or
missing telemetry remains unavailable.

The Codex MCP server binds write-capable tools to the process startup project
root and the current Recovery Envelope actor, Work, and claim. Cross-project
roots, mismatched actor/claim/Work, and read-only envelopes are rejected before
the CLI runs. The plugin manifest now declares both Read and Write, matching the
tools it exposes.

## Open Gates

- the running Codex App thread must start after the updated plugin installation
  before native lifecycle behavior can be observed;
- Codex `exec` currently lacks the personal-plugin candidate surface in the
  measured host configuration;
- matched baseline/candidate runs require at least three segments per exact
  match key and a second real provider;
- longitudinal `accepted Work / compaction` and token/Work remain unavailable;
- the current local legacy State does not yet persist an active compiled Skill
  lock; the Codex plugin supplies a content-bound plugin rule lock;
- v1-to-v6 project migration and the Platform DAG policy remain separate gates.

M10-11 remains open. Contract tests and local smoke measurements do not grant a
cross-project, cross-provider, token-saving, or release-completion claim.

Repository verification after the implementation discovered `1,895` tests:
`1,864` passed, `31` conditional skips, and `0` failures in `443.167 s`. The
unified repository verifier, Ruff, plugin validator, schema governance, and
document lifecycle gates also passed.
