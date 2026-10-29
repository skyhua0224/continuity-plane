---
name: continuity-collaboration
description: Use whenever work has several steps, involves another agent or person, must survive compaction or a new session, or when you take over, dispatch, wait for, review or report on a task. Keeps shared task cards, worker runs, messages and handoffs in Continuity through its MCP tools.
---

# Continuity collaboration

Use the `continuity` MCP tools; run the `continuity` CLI only if they are unavailable.

## Every session
- Start or resume: `continuity_start` (your cards, unread messages, recent changes).
- Taking over a project: `continuity_handoff_show`, then `continuity_task_list`.
- Before you stop or the context fills: `continuity_handoff_write(plan, next_action, pending)`.
- Finding code: `continuity_context_lookup` before broad grep.

## As controller
- One card per piece of work: `continuity_task_add(task_id, title, body, assignee)`. The body says what to do, the scope and how to check it.
- Change cards with `continuity_task_update` (status, next_action, assignee, body).
- Send to a worker: `continuity_worker_dispatch(task_id)` previews; add `launch=true` to start.
- Wait: start `continuity worker wait --project-id P --task-id T` as a background shell task and end your turn; it wakes you when the worker ends. `continuity_events_wait()` is a single check across your projects, never a loop.
- Read the result: `continuity_worker_report(task_id)` gives a summary and headings; page with `offset`. Then update the card or `continuity_review_request`.
- Rare operations (rules, references, quota, queue, batches): `continuity_plan(action=…)`, parameters via `continuity_help(topic=…)`.

## As worker
- `continuity_progress(note)` at milestones.
- `continuity_report_final(summary, checks, unverified, branch, commit)` when done: it saves the report, marks the card ready for review and tells the dispatcher.
- Questions that block you: `continuity_inbox(action="send", recipient=<dispatcher>, body=…)`.

## Limits
- Cards record intent; they do not grant merge, push, deploy, paid-provider or account access.
- A progress note is not the end of the task; stop only for a real blocker or a decision.
- See [collaboration workflow](references/collaboration.md) for takeover, queues and host restarts.
