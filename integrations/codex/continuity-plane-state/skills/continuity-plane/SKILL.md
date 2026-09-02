---
name: continuity-plane
description: Bounded continuity for projects with a .continuity directory.
---

# Continuity Plane

- Use a healthy packet directly; do not re-read STATUS, MASTER, AGENTS, or SKILL files.
- Call `continuity_inspect` at most once per turn when no healthy packet was injected.
- After a successful inspect, answer from its result; do not read `.continuity` or governance files unless the user asks to diagnose a mismatch.
- Call `continuity_resume` once, only before an explicit State write; never repeat it after success.
- Both auto and observe modes never block normal project work; adapter failure continues without old Work.
- Answer questions directly. Ideas do not replace Work. Compaction has no replay or recovery narration.
- Strict mode applies only when the project explicitly opts in, for declared irreversible effects.
- State, source, and verified evidence outrank memory and prose. Never edit State storage directly.
