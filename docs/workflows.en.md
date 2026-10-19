# Workflows by role

[中文](workflows.md) · [Usage](../USAGE.en.md) · [Detailed parameters](field-workflows.md)

Use after installation. Replace IDs, paths, models and test commands. Cards plus shared handoffs may be sufficient: Todo, claim and inbox are not compulsory rituals. CLI, SSH wrappers and MCP are alternative entry points.

## Solo work across compaction

Six commands establish a readable return point in a new project:

```bash
continuity init --root . --project-id demo
continuity collaboration register --project-id demo --control-root . --repository-root .
continuity whoami --root . --assignee controller --controller controller
continuity handoff write --project-id demo --plan 'Fix the current regression before the next item' --next-action 'Run regression tests'
continuity handoff show --project-id demo
continuity collaboration packet --project-id demo --assignee controller
```

Enable actual host hooks next. These commands alone do not cause native compaction or injection. Keep the next action, hard constraints, verification entry points and pending decisions small. A new session reads packet/handoff instead of producing a report before resuming.

## One controller and two workers

Assume an authorized native client and two prepared independent checkouts. Replace all paths, model ID and task-specific checks. These ten commands go from initialization to two report reads:

```bash
continuity init --root . --project-id demo
continuity collaboration register --project-id demo --control-root . --repository-root .
continuity collaboration task add --project-id demo --task-id task-a --lane-id a --mode patch --priority p1 --title 'Fix A' --objective 'Complete a bounded fix' --next-action 'Run tests' --exit-criterion 'Verifiable evidence' --assignee worker-a --worktree /worktrees/task-a --allowed-file src/a.c --body 'Inspect related symbols, fix A and record the actual checks'
continuity collaboration task add --project-id demo --task-id task-b --lane-id b --mode patch --priority p1 --title 'Verify B' --objective 'Verify boundary cases' --next-action 'Run tests' --exit-criterion 'Reproducible report' --assignee worker-b --worktree /worktrees/task-b --allowed-file tests/ --body 'Verify B only within authorized scope'
continuity worker configure --model primary --provider codex --provider-model YOUR_MODEL_ID
continuity dispatch --project-id demo --task-id task-a --model primary --controller controller --effort high --timeout-seconds 86400 --idle-timeout-seconds 5400
continuity dispatch --project-id demo --task-id task-b --model primary --controller controller --effort high --timeout-seconds 86400 --idle-timeout-seconds 5400
continuity collaboration wait --project-id demo --since 0 --event attempt.finished --event attempt.timeout --event dispatch.policy-stopped --timeout-seconds 30
continuity report task-a --project-id demo
continuity report task-b --project-id demo
```

This basic path runs dispatch in the foreground, sequentially, so both reports are ready before reading. For concurrency add --background to each dispatch, then wait for each required task ID before reading reports. A single matching event does not prove all workers finished. Timeout is not worker failure; an early report read may legitimately say not attached yet. No Git merge/push happens here.

Task bodies must include your actual build/test/review expectations. `--worktree` must exist on the execution host; prepare isolated checkouts before parallel edits. Declared allowed-file is not OS enforcement.

### Optional preparation and existing scripts

Existing SSH launch/resume/report wrappers can remain primary. You can inspect rather than manually concatenate context:

```bash
continuity rules export --project-id demo --audience worker --format text
continuity collaboration prompt --project-id demo --task-id task-a --assignee worker-a
continuity collaboration packet --project-id demo --assignee worker-a
```

Use `dispatch --dry-run` for a plan, not a liveness claim. `resume task-a 'Continue verified progress'` uses recorded native references; not every custom adapter supports resume. Effort passes through, and provider support differs. Persistence and authentication stay on the owning host/profile.

Pasteable handoff convention:

> Start by checking your identity, packet, rules and task body; read inbox/code routes when relevant. Finish with the final report, commit and actual verification, explicitly listing unverified scope. Hand back to the controller. Card status grants no push/merge authority.

Normal successful attempts become ready-for-review; failed/interrupted attempts become blocked. Alpha28 attaches the current attempt's final report even after early card updates, without regressing manual status. Stale attempts or reassigned executors cannot replace the new attachment. See [validation](validation.en.md) for evidence scope.

### Integration and approval

Use `task update --verification-json` for evidence bound to the actual commit. Passed and unverified checks are distinct. `batch-add` stores cards, baseline, result, verification and optional metrics; `decision-add` records real approval sources and corrections. `task merged TASK --batch BATCH` consumes an existing batch; it does not execute Git.

Inspect `continuity help batch-add` before building larger records. Never promote placeholder commits or unexecuted checks into evidence. Gaps remain visible; overrides need reasons, and external actions still need authorization.

## A team

Register on the agreed authority host and upgrade every dispatcher before opting into fencing. Five operations illustrate the boundary; replace `FENCE` with the returned project coordination token, not a provider key:

```bash
continuity controller acquire --project-id demo --owner controller --ttl-seconds 10800 --client-limit codex=5
continuity controller status --project-id demo --owner observer
continuity controller renew --project-id demo --owner controller --token FENCE --ttl-seconds 10800
continuity handoff show --project-id demo
continuity collaboration task list --project-id demo --format brief
```

A second controller can read; protected dispatch, batch and integration operations require the current lease. Route explicitly authorized integration commands through `controller run`; arbitrary native agents and Git outside it are not protected.

First acquire enables guarded ledger v4. Expiry does not kill workers or grant them new dispatch authority. Unknown operations are not replayed after takeover. Clearing reservations/operations requires exact IDs and reasons. See [remote deployment](deployment.en.md).

## Rules, sources and lessons

Keep rules canonical, with project/global scope and worker/reviewer/controller audiences. Check generated mirrors through `rules check-mirror`; long policies use hashed attachments, never silent clipping.

`reference list/add/record` records primary sources and adoption reasons; `lesson add/list` preserves experience and corrections. Import the repository index with `reference import-tsv --file docs/authority/source-index.tsv`; generate a new comparison file with `reference export-tsv --file source-index.export.tsv`. Existing files are never overwritten. Key conflicts reject the entire import; imports declare candidates, not reading evidence. See [Alpha28](release-alpha26.md) for columns and identity setup.

`todo from-cards` is a view, open-state by default. A separate sequential Todo is optional. Inbox is useful when delivery and acknowledgement matter; existing reliable handoffs need not add a message ritual. Adoption should be measured by useful outcomes, not use of every command.
