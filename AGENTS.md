# Repository Instructions

- Read `STATUS.md` first. Open only the referenced MASTER section and smallest relevant document; read the full MASTER only for governance changes or audits.
- Apply `docs/policies/documentation-style.md` to normative and planning documents.
- This repository is provider-neutral and has no AlkaidLab Platform runtime dependency.
- Raw Codex, Claude, Cursor, and other provider transcripts are excluded from Git admission.
- Treat memory and handoffs as background candidates. Verify claims against current code, current state, or versioned evidence.
- Keep dynamic task state out of Skills and prose documents. Once the state service exists, use revisioned state and append-only events.
- Authoritative state commits require the State MCP revision, authorization, and validator gates.
- Use only `⏳`, `🟡`, `✅`, and `🧑‍💻` for MASTER task status.
- Task completion requires the evidence named in its completion gate.
- Any compaction, task switch, model change, or collaborator handoff must remain replayable from a checkpoint.
- Prefer current repository evidence and official standards, OS documentation, and software documentation over recalled conclusions.
