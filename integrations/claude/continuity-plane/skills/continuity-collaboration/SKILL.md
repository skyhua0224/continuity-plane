---
name: continuity-collaboration
description: Keep explicit multi-step work and collaborator handoffs in Continuity Todo/cards; do not invoke for every small edit or status question.
---

# Continuity collaboration

- When replacing a controller/model, run `controller.start` once for plan, report/tool profile, approved memory and next action; avoid inspect/resume repetition.
- Track explicit multi-action work through cards and a card-backed Todo. Retain completed evidence and commitments; switch items with `todo.activate` and a reason.
- Prefer `continuity_plan` and discover parameters with `continuity_help`; CLI fallback is `continuity help OPERATION`.
- Profiles/memory guide habits but grant no permission. Import native notes only from exact owner/profile files with consent; they stay candidates on that member/host unless separately shared.
- Consult [collaboration workflow](references/collaboration.md) for takeover, dispatch, acknowledgements and full rules. Session return points and controller handoffs differ; rejected dispatch is not a running worker.
- Use MCP `worker.queue.enqueue` or CLI `worker.dispatch --enqueue` when work must wait for capacity/quota, and a single CLI `worker.queue run` process to launch it; do not create competing filler loops. Use `task.update --batch` for shell-sensitive multi-card changes.
- After a host reboot, list stale reservations and reconcile them under the controller lease before treating a capacity error as authoritative. MCP itself is host-owned and may require a new host connection.
- Record stable identity, checkout, scope, next action and checks. Keep artifacts persistent; native account/session work stays on the owning profile and host.
- Use `reference.list/record` for reviewed primary sources, versions and adoption reasons.
- Return commit-bound checks, report and unverified scope. Progress is nonterminal; stop for real blockers, decisions or scope boundaries.
- Planning metadata requires no State claim. If MCP fails, try the equivalent CLI once and preserve a bounded handoff; ordinary authorized work can continue.
- Reserve `continuity_resume` for explicit State writes. No card grants State edits, paid providers, merge, publish or deploy.
