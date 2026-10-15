---
name: continuity-collaboration
description: Track explicit multi-step work and collaborator handoffs with Continuity Todo and task cards; use for maintaining an execution plan, not every single edit or status question.
---

# Continuity collaboration

- Reuse a current task packet; expand only missing details. Do not perform an inspect/resume ritual every turn.
- For an explicit multi-action request, maintain a Todo and link actionable cards by task id. Preserve completed evidence when the user adds or reprioritizes work.
- Prefer `continuity_plan` for metadata operations; use `continuity_help` for exact parameters. CLI fallback: `continuity help overview`, then a topic such as `todo.compile` or `task.add`.
- For worker dispatch, full rule delivery or a registry on another host, read [collaboration workflow](references/collaboration.md) on demand. A packet's rule summaries are not the complete policy.
- Supply an absolute root or registered project id, plus data root when nondefault. Record assignee, worktree, next action, scope and verification before handing off; queued cards are not dispatched workers.
- Retain tasks, reports, thread references and checkpoints in the persistent work/data directory, not a temp mount; `storage.inspect` reports observed filesystem risks. Run account/authentication operations within this Claude profile on its own machine. Remote coordination exchanges permitted results, not transplanted private profile state.
- On completion, attach evidence and take the next unfinished item within the user's agreed stage. Brief progress notes do not end execution; stop for a real blocker, required decision or the requested boundary.
- Reconcile explicit plan changes with `todo.activate` and a reason; do not discard earlier commitments or pretend unverified work is done.
- For consequential design, reuse applicable sources via `reference.list` and record reviewed versions and adoption reasons via `reference.record`. See [reference workflow](references/sources.md) when a task calls for standards, OS/library documentation or implementation comparisons.
- Todo/card/inbox/search operations need no State claim. Missing tools or metadata do not make project work read-only; try the matching CLI once and continue with a bounded handoff if unavailable.
- Use `continuity_resume` only before an explicit authoritative State write. Do not edit State storage, invent completion authority, launch paid workers, merge or deploy merely because a card says so.
