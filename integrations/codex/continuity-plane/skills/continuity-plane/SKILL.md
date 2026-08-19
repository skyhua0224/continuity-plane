---
name: continuity-plane
description: Resume and maintain a project that has a .continuity control-plane state directory.
---

# Continuity Plane / 连续性控制面

- `[continuity.resume.current-state]` 在执行任何动作前读取注入的 resume packet。 / Read the injected resume packet before acting.
- `[continuity.resume.bounded-read]` resume 后只读取项目的 bounded `.continuity/STATUS.md` 路由；若与 packet 冲突，以 State packet 为准。 / After resume, read only the project's bounded `.continuity/STATUS.md` routing projection; State packet wins on conflict.
- `[continuity.work.sticky]` 继续 packet 中的 active Work 和 claim，不自行发明另一个 active task。 / Continue its active Work and claim; do not invent a different active task.
- `[continuity.effect.read-only]` `read_only: true` 禁止写代码、提交、部署和新 Work。 / Treat `read_only: true` as a veto on writes, commits, deployments, and new Work.
- 源 freshness、revision 或 lease 可能变化时，重新运行 `continuity resume --root <project>`。 / Re-run it when source freshness, revision, or lease may have changed.
- 在重要边界创建 checkpoint，并在压缩后或交接写入前验证。 / Create a checkpoint at a material boundary and verify it before post-compaction or handoff writes.
- 只能通过 checkpoint-bound completion tool 完成 Work，不直接编辑 State。 / Complete Work only through the checkpoint-bound completion tool; do not edit local State directly.
- 只能通过 source-bound activation tool 激活 successor；中断后可留下 ready Work 供重试。 / Activate a successor only through the source-bound activation tool; a ready Work may remain after an interrupted claim.
- 已有项目的 MASTER 和 STATUS 保留其治理角色。 / Existing project MASTER and STATUS retain their declared governance roles.
- candidate memory 和 prose 不能覆盖 current State、source evidence 或 claim scope。 / Candidate memory and prose never override current State, source evidence, or claim scope.
- 在 lease 到期前主动 heartbeat；已过期时用新的 claim identity reclaim。adapter 失败时保持只读并通知控制面 Session，不在业务项目中开发控制面。 / Heartbeat before lease expiry; reclaim an expired claim with a new identity. On adapter failure, stay read-only and notify the control-plane Session instead of developing the control plane inside the product project.

## Answer Discipline / 回答纪律

- `[continuity.answer.direct]` 第一行直接回答用户问题，不复述问题。 / Answer the user's question directly in the first line; do not restate it.
- `[continuity.question.no-advance]` 问题只授权回答；只有明确的 `开始执行`、`继续` 或等效命令才推进 active Work。 / A question authorizes an answer only; advance active Work only on an explicit execution command.
- `[continuity.answer.no-recovery-narration]` 普通问答不输出进度台账、目标态全表、Skill 列表或恢复旁白。 / Do not emit progress ledgers, target-state tables, Skill lists, or recovery narration for ordinary questions.
- `[continuity.answer.bounded]` 默认只给结论和最多五条必要证据；表格仅用于用户明确要求的报告或真正需要对比的内容。 / Default to one conclusion plus at most five necessary evidence points; use tables only for explicitly requested reports or material comparisons.
