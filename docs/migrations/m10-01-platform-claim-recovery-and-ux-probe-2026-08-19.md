# M10-01 Platform Claim Recovery And UX Probe

Version: 1  
Date: 2026-08-19  
Status: verified local-embedded shadow recovery; longitudinal gate remains open

```yaml
document_id: context.m10-01-platform-claim-recovery-and-ux-probe
document_revision: 1
change_type: evidence
authority_ref: verification-run://platform-shadow/revision-29
supersedes: null
affected_tasks: [M5-08, M8-02, M10-01, M10-11]
next_review: M10-01
```

## Scope

The Platform shadow pilot exposed an expired active claim in typed-state v1.
`resume` correctly returned `lease_valid=false` and `read_only=true`. The
shared v6 Work Ledger already provided fenced heartbeat and reclaim, while the
local v1 adapter had no recovery operation.

`context.state.claim.recovery` now provides a bounded compatibility bridge:

- `heartbeat` extends only a live claim;
- `reclaim` accepts only an expired active claim, marks the old identity
  `expired`, and creates a new active claim identity with identical actor,
  Work, and scope;
- both paths use authorization, expected revision, one append-only Event,
  deterministic request identity, and trusted adapter time;
- v6 State rejects this bridge and continues to use the fenced shared lifecycle
  contract.

The pre-mutation export bundle is
`/tmp/alkaidlab-platform-before-reclaim-20260819T2200.continuity.zip`, SHA-256
`7904be60b852b820dc892ed493aae695b655bbd5e1fc930076cbf256f87f6eeb`.
It records Platform revision/event `27/27`.

## Live Recovery

| Field | Result |
|---|---|
| old claim | `claim-n-69-03`, status `expired` |
| successor claim | `claim-n-69-03-reclaimed-1`, status `active` |
| active Work | `N-69-03` |
| reclaim revision/event | `28/28` |
| plugin heartbeat revision/event | `29/29` |
| source freshness | `true` |
| lease validity | `true` |
| read-only | `false` |
| checkpoint | `artifact://sha256/f352b53fd13cba8d11f37123d5fe91b840e56526abb0c98c0591efe1f47d18f7` |

The existing Platform Session received the new claim, called `resume` and
`checkpoint verify`, and continued `N-69-03`. It did not edit Continuity Plane.

## Twenty-Hour Session Probe

The source is the Platform thread
`019fe20b-ccf5-7b61-95e2-6cacf637ed3e`, measured from 2026-08-18 16:14Z to
2026-08-19 12:14Z.

| Metric | Result | Interpretation |
|---|---:|---|
| compactions | `5` | real provider events |
| compaction intervals | `181/197/229/482 min` | median `213 min` |
| prior 258K-window median | `38 min` | historical unmatched context-window period |
| active Work/claim restored | `5/5` | no wrong-leaf restart observed |
| user wrong-task correction after compaction | `0/5` | consistency signal |
| first post-compact input | `54.8-56.8K tokens` | bounded recovery target remains unmet |
| pre-compact input | `807-905K tokens` | host compaction threshold vicinity |
| recovery narration emitted | `5/5` | seamless interaction target failed |
| full repository after recovery adapter | `1851` discovered; `1820` passed; `31` conditional skip; `0` failed; `378.759 s` |

The context window changed from approximately 258K to 950K. The longer
compaction interval therefore cannot be attributed to Continuity Plane. A
matched provider/model/window/task-class baseline is still required by M10-11.

## Answer Contract

The Platform plugin Skill and ignored STATUS now require:

- answer the question in the first sentence;
- questions authorize answers only;
- explicit execution commands advance active Work;
- ordinary questions omit recovery narration, Skill lists, progress ledgers,
  target-state tables, and restatement of the question;
- default responses contain one conclusion and no more than five necessary
  evidence points;
- tables are used for explicitly requested reports or material comparisons.

This policy addresses answer relevance and repetition. It does not claim model
quality improvement until a matched response evaluation is collected.

## Open Gates

- migrate project authority from typed-state v1 to v6 without inventing a Work
  DAG or losing the existing flat historical Work identities;
- generate STATUS from current State so lease/read-only fields cannot drift;
- connect a supported provider lifecycle hook for automatic resume/checkpoint;
- reduce first post-compact input toward the 4-12 KB Execution Packet contract;
- complete M10-11 matched longitudinal and response-quality evaluation.
