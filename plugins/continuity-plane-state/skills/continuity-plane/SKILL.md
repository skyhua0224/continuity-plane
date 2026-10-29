---
name: continuity-plane
description: Use when a request has several steps, when you were dispatched onto a Continuity card, when work must outlive this Codex session, or when you hand off, wait for, review or report on work. Tracks cards, runs, messages and handoffs through the continuity MCP tools.
---

# Continuity Plane for Codex

Call the `continuity` MCP tools. Fall back to the `continuity` command only when the tools are missing, and say so in your report.

## Dispatched onto a card (most Codex sessions)
1. `continuity_start` with the project_id and task_id from your prompt: card, messages, changes.
2. Work in the checkout the card names. Grep for a known name; use `continuity_context_lookup` when grep would return many matches.
3. `continuity_progress(note=…)` after each milestone, so the dispatcher sees where you are.
4. Finish with `continuity_report_final(summary, checks, unverified, branch, commit)`. It stores the report, sets ready-for-review and messages the dispatcher. Do not write report files or edit the card by hand.
5. Need a decision? `continuity_inbox(action="send", recipient=<dispatcher>, body=…)`, then continue with what you can.

## Running the project yourself
- Plan with `continuity_task_add` (task_id, title, body, assignee) and adjust with `continuity_task_update`.
- `continuity_worker_dispatch(task_id)` shows the plan; `launch=true` starts the worker.
- To wait for a worker, run `continuity worker wait --project-id P --task-id T` in the background (exit 0 = a terminal event, 3 = timeout). `continuity_events_wait()` only checks once; calling it repeatedly re-reads your context each time.
- `continuity_worker_report` returns a summary with section offsets; request `offset` for more.
- Leave `continuity_handoff_write(plan, next_action, pending)` before you stop; a successor reads `continuity_handoff_show`.
- Anything else: `continuity_plan(action=…)` with parameters from `continuity_help`.

## Boundaries
Cards describe work; they never authorize merge, push, deploy, paid providers or account access. Progress notes do not end a task. Takeover, queues and restarts: [collaboration workflow](references/collaboration.md).
