---
name: continuity-collaboration
description: Keep explicit multi-step work and collaborator handoffs in Continuity Todo/cards; do not invoke for every small edit or status question.
---

# Continuity collaboration

- Continue from a current packet, loading missing details on demand. Avoid repetitive inspect/resume calls.
- Track explicit multi-action work through cards and a card-backed Todo. Retain completed evidence and commitments; switch items with `todo.activate` and a reason.
- Prefer `continuity_plan` and discover parameters with `continuity_help`; CLI fallback is `continuity help OPERATION`.
- Consult [collaboration workflow](references/collaboration.md) for takeover, dispatch, consumer acknowledgements and full rule delivery. A session return point and a shared controller handoff are distinct. Rejected dispatch is not a running worker.
- Set the root/project, stable executor identity, checkout, scope, next action and checks before handoff. Rule summaries do not replace complete audience rules.
- Use persistent work/data storage for reports and recovery; inspect unknown locations with `storage.inspect`. Native account/auth/session operations stay inside this owning Claude profile and host, never a transplanted profile directory.
- Reuse primary documentation with `reference.list`; record reviewed versions and rationale with `reference.record`. See [source references](references/sources.md) for standards, OS/library docs and implementation comparisons.
- Return commit-bound checks, report and unverified scope, then continue the agreed stage. A progress update is not completion; stop for actual blockers, decisions or scope boundaries.
- Planning metadata requires no State claim. If MCP fails, try the equivalent CLI once and preserve a bounded handoff; ordinary authorized work can continue.
- Reserve `continuity_resume` for explicit State writes. No task card grants authority to edit State, start paid providers, merge, publish or deploy.
