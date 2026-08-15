# M5-07 Continuity Incident Correction

Version: 1  
Date: 2026-08-16  
Status: live regression recorded / detector verified

```yaml
document_id: context.m5-07-continuity-incident-correction
document_revision: 1
change_type: correction
authority_ref: observation://dogfood/m7-02-stop
affected_tasks: [M3-08, M5-07, M5-08, M7-03, M8-09]
next_review: M8-09
```

## Incident

The assistant terminated after M7-02 while M7-03 was selected, ready and not
blocked. The reconstructed continuation obligation required `continue`; the
observed action was `assistant-final`. The live receipt therefore records one
`premature-stop` veto failure with status `regressed`.

The incident is stored separately from the fixed M3-08 benchmark. Historical
fixture results with `premature_stop_count=0` remain immutable and do not
override this live observation.

## Correction

`context.continuation-obligation/v1alpha1` reconstructs the selected ready
work, campaign, revision, action and observation interval. The independent
`context.continuity-incident/v1alpha1` receipt binds that obligation to the
termination digest, remaining ready work, typed blocker state, detection
source and append-only trace event. Digests, duplicate admission, conflicting
replay, campaign and revision drift, future obligations, invalid blockers and
forged continuation decisions fail closed.

## Verification

| Gate | Result |
|---|---|
| focused behavior | `12/12` continuity incident tests pass |
| live classification | expected `continue`; actual `stop`; status `regressed` |
| replay | deterministic; duplicate admission idempotent; conflicting replay rejected |
| authority | trace, detector and receipt State write/completion authority `0` |
| source separation | fixed benchmark and live-observation metrics remain distinct |
| evidence | [`live-premature-stop-2026-08-16.json`](../../experiments/dogfood/live-premature-stop-2026-08-16.json) and bound trace receipt |

## Remaining Obligation

The detector makes the failure observable and replayable. It does not dispatch
the next task. M8-09 must enforce the unattended select, claim, execute,
verify, complete and continue loop and demonstrate zero premature stops across
at least three consecutive required leaves.

## Reproduction

```text
.venv/bin/python tools/generate_continuity_incident.py
.venv/bin/python -m unittest tests.test_m5_07_continuity_incident -q
```
