# M10-09 Local Lifecycle Adapter Acceptance

Version: 1  
Date: 2026-08-18  
Status: verified local-embedded alpha adapter

```yaml
document_id: context.m10-09-local-lifecycle-adapter-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m10-09-local-lifecycle-adapter-v1
supersedes: null
affected_tasks: [M2-06, M8-09, M10-01, M10-09]
next_review: M10-09
```

## Scope

The local lifecycle adapter provides canonical source attach, bounded resume,
immutable checkpoint create/verify, and atomic Work completion. Completion is
authorized through State MCP, binds the current checkpoint and content-addressed
evidence, and changes Work and claim state in one CAS Event.

The personal Codex plugin exposes `continuity_resume`, `continuity_checkpoint`,
and `continuity_work_complete` through MCP. The current Codex plugin validator
does not admit a manifest `hooks` field; SessionStart and PreCompact automation
remain open provider-adapter work.

## Verification

| Gate | Result |
|---|---|
| resume packet | strict `context.resume-packet/v1alpha1` schema and registry hash pass |
| checkpoint | repeated publication has one content identity; current authority verify passes; source drift rejects |
| completion | Evidence import, Work `completed`, and claim `released` use one revision/Event |
| idempotency | repeated completion returns `already-completed`; Event count remains unchanged |
| missing evidence | request rejects before State mutation |
| focused regression | `65` tests passed; `0` failed; `1` PostgreSQL parity test conditionally skipped |
| plugin | manifest and Skill validation pass; MCP tool listing contains all three lifecycle tools |

## Platform Shadow Receipt

The first external dogfood completion advanced the local Platform shadow State
from revision/event `2/2` to `3/3`. Diagnostic Work `N-67` became `completed`,
`claim-n-67` became `released`, and three evidence objects bound the pre-completion
checkpoint, live transfer receipt, and durable verdict. Repeating the same
request produced no Event. The post-completion checkpoint is
`artifact://sha256/ed8ec78dc1c955d63297a3459f6730e16e9033b3602d0ce85821b298f8aea754`
at revision/event `3/3`.

This receipt closes the imported diagnostic leaf. It does not change the
canonical Platform N-67 completion gate or authorize N-42 to consume retained
BBR state.

## Boundaries

- Local completion requires explicit local workflow authorization and verified
  evidence files. Team completion continues to require the M8-09 shared v6
  claim fence, lease epoch, independent verification decision, and claim-evidence
  resolver.
- The plugin has no State authority beyond the CLI request authorized for the
  current local actor.
- A source-bound canonical STATUS does not copy its own live State revision;
  that value is read from State MCP to avoid source-refresh revision cycles.
- Creating and claiming the next canonical Work is not part of this adapter.
- Export, import, rollback, and the three-platform migration matrix remain open.

## Reproduction

```text
.venv/bin/python -m unittest \
  tests.test_m2_05_state_mcp \
  tests.test_checkpoint_cli \
  tests.test_work_completion_cli -q
continuity checkpoint verify --root /path/to/project
continuity work complete --root /path/to/project \
  --work-id WORK-ID --claim-id CLAIM-ID --actor-ref ACTOR-ID \
  --evidence-file /path/to/verification-receipt.json
```
