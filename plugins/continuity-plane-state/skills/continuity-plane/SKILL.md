---
name: continuity-plane
description: Bounded continuity for projects with a .continuity directory.
---

# Continuity Plane / 连续性控制面

- `auto` 为默认策略；`observe` 仅记录。auto and observe modes never block normal project work. / `auto` is the default; `observe` only records. Neither blocks normal work.
- 健康 packet 已注入时直接使用；do not re-read STATUS, MASTER, AGENTS, or SKILL files。只展开当前请求需要的 opaque ref。 / Use a healthy injected packet directly and expand only a required opaque ref.
- 仅 when no healthy packet was injected，读取有界 `.continuity/STATUS.current.md`；不可用时继续正常工作，不复活旧 Work。 / Read the bounded status projection only without a healthy packet; otherwise continue normally without reviving old Work.
- startup/resume 只保存 return point：问题直接回答，Idea 不替换 Work，执行请求才推进。`source=compact` 才续接同一轮，不复答或输出恢复旁白。 / Startup follows current intent; compact resumes without replay or narration.
- 普通 Work 的手工 resume、claim、heartbeat、checkpoint、complete 和 transition 目标为 `0`；adapter 失败时静默降级。 / Normal Work targets zero manual Continuity operations; adapter failure degrades silently.
- Strict mode applies only when the project explicitly opts in through a verified profile. It gates selected irreversible effects with a precise `failed_gate`; ordinary development is excluded. / `strict` 仅保护选定的不可逆副作用。
- current State、当前源码和验证证据高于 memory、prose、Skill 与 provider summary；禁止手改 SQLite。 / Current State, code, and verified evidence outrank memory and prose; never edit SQLite directly.
- 第一行直接回答；未请求时不输出台账、全表、Skill 列表或恢复说明。 / Answer directly; omit ledgers, large tables, Skill lists, and recovery narration unless requested.
