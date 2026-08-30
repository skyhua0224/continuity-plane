---
name: continuity-plane
description: Resume and maintain a project that has a .continuity control-plane state directory.
---

# Continuity Plane / 连续性控制面

## Default behavior / 默认行为

- `observe` 是默认策略，只记录、不给模型增加状态正文、不阻断命令。`auto` 只在健康状态下提供有界恢复和 checkpoint；observe and auto modes never block normal project work. / `observe` is the default and records without injecting state or blocking commands. `auto` adds bounded recovery and checkpoints only while state is healthy.
- 收到 current、source-fresh、checkpoint-verified 且 writable 的注入 packet 后，直接使用它；do not re-read STATUS, MASTER, AGENTS, or SKILL files。只在 packet 的 opaque reference 对当前请求确实必要时有界展开。 / After a healthy packet is injected, use it directly and expand only an opaque reference required by the current request.
- 仅 when no healthy packet was injected，读取 `.continuity/STATUS.current.md`；该投影不可用时继续正常项目工作，不猜测旧 Work。 / Read `.continuity/STATUS.current.md` only when no healthy packet was injected. If it is unavailable, continue normal project work without inventing or reviving Work.
- 新 Session 的 packet 只保存 return point。问题只回答问题；Idea 不替换 active Work；只有明确的执行请求才推进 `next_action`。 / A new-session packet preserves a return point. Answer questions directly, keep Ideas from replacing active Work, and advance only when the current request authorizes execution.
- `source=compact` 表示同一轮被压缩；从 packet 的 `next_action` 丝滑续接，不复答 acknowledged input，不输出恢复旁白。 / `source=compact` continues the interrupted turn from `next_action` without replaying acknowledged input or narrating recovery.
- 普通 Work 的手工 `resume`、claim、heartbeat、checkpoint、complete 和 transition 次数目标为 `0`；宿主或 adapter 自动处理，失败时静默降级为正常 Session。 / Normal Work targets zero manual Continuity operations; host adapters handle them and silently degrade to a normal session on failure.

## Strict opt-in / 严格模式

- Strict mode applies only when the project explicitly opts in through its verified profile. It is intended for selected irreversible production effects, not ordinary development, tests, commits, PRs, merges, or routine deployment flows. / `strict` 仅由已验证 Project Profile 显式启用，用于选定的不可逆生产副作用；普通开发、测试、提交、PR、合入和常规部署不默认进入严格模式。
- 在 strict 模式中，`read_only: true`、stale source、invalid lease 或 invalid checkpoint 可以否决受保护副作用；拒绝必须返回具体 `failed_gate`。 / In strict mode, read-only, stale-source, lease, and checkpoint failures may veto protected effects and must return a precise `failed_gate`.
- strict 恢复只使用受控的 heartbeat/reclaim、checkpoint-bound completion 和原子 dependency transition；不得手改 SQLite，也不得拆分原子 transition。 / Strict recovery uses controlled heartbeat/reclaim, checkpoint-bound completion, and atomic dependency transition; never edit SQLite or split the transition.

## Authority and answer boundary / 权限与回答边界

- candidate memory、prose、Skill 和 provider summary 不得覆盖 current State、当前源码或验证证据。 / Candidate memory, prose, Skills, and provider summaries never override current State, current code, or verified evidence.
- 第一行直接回答用户问题；默认只给结论和必要证据。未请求时不输出台账、全表、Skill 列表或恢复说明。 / Answer directly in the first line and keep evidence bounded. Do not emit ledgers, large tables, Skill lists, or recovery narration unless requested.
