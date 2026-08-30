# Continuity Plane Status

版本：revision 113  
日期：2026-08-30  
canonical plan：`MASTER.md`

## 当前安全姿态与收口目标

| 字段 | 值 |
|---|---|
| integration posture | `observe`；本机 core/search/state 三插件显式启用；命令门禁 0 |
| 业务影响 | Platform、ProjectCompute、Foundation Account 不受命令门禁影响 |
| 收口目标 | M11-00：Zero-friction continuity core |
| 收口条件 | 三项目各 `3` 段 matched baseline/candidate；旧 Work 复活、重复回答、误阻断为 `0`；恢复读取和 history-heavy token 达到目标降幅 |
| 未达标策略 | 保留本机非阻断 dogfood；不发布新的公共默认启用版本 |

## 当前状态

| 字段 | 值 |
|---|---|
| 当前 Campaign | M10 跨项目发布 |
| active work | M10-01：跨项目 shadow pilot（🟡） |
| next action | 建立可续跑的自然 1M/900K shadow runner，再按 accepted Work 重复 matched A/B |
| hard blocker | Platform 与 ProjectCompute input-token gate 均未达 `30%` |
| repository mode | internal development repository + fresh-history public mirror |
| production state | GitHub/PyPI alpha.9 prerelease published；本机 current snapshot 三插件 enabled；shared pilot planned |

## 已验证摘要

| 对象 | 状态 |
|---|---|
| Evidence index | [`docs/reports/verified-state.md`](docs/reports/verified-state.md) |
| Verified campaigns | M0-M8 core 与 M9-01..07 见 Evidence index；M9-08..11 planned |
| alpha.9 publication | GitHub prerelease、PyPI、脱敏镜像、wheel/sdist、`SHA256SUMS`、CI 和本地 CLI/plugin 已通过 |
| Coordination/views | shared Work、forge、notification、Project Graph、2,000-node Impact gates passed；authority violation `0` |
| Release baseline | Linux/macOS/Windows native matrix `18/18`；local state bundle export/import/rollback available；public privacy scan `0` findings |
| Documentation audit | README、USAGE、CHANGELOG、public docs、templates 与 policy 全文审阅；公开 Markdown 内部阶段标记 `0`，本地链接缺失 `0`，外部链接 `8/8` 可达；repository verification passed |
| M11-00 zero-friction reset | 本机 core/search/state 显式启用；core 无 Skill/MCP/命令门；三仓 source-control/remote-effect 无副作用 shell probe `6/6` 到达执行层；State MCP 在新 Codex task 可见；54/54 profile/lifecycle tests 通过；[pre-reset baseline](experiments/evidence/m11-00-live-baseline-20260830.json) |
| M11 hook efficiency | `40` 组：calls `-33.33%`，model context `-83.73%`，stdout `-85.18%`，wall p50/p95 `-25.60%/-29.98%`，deny/stop `0/0`；[receipt](experiments/evidence/m11-00-codex-hook-efficiency.json) |
| M11 Platform matched A/B | `3+3`：input/output/tool-output 中位 `-12.14%/-28.84%/-41.46%`，veto `0`，input gate failed；[comparison](experiments/evidence/m11-live/platform-ultralight/platform-comparison.json) |
| M11 ProjectCompute matched A/B | `3+3`：input/output/tool-output 中位 `-2.36%/-13.70%/+2.62%`，veto `0`，input gate failed；[comparison](experiments/evidence/m11-live/projectcompute-ultralight/projectcompute-comparison.json) |
| M11 bounded retrieval | current tracked worktree、revision/hash、完整 JSON budget 与 CLI `3/3`；默认自动 Skill adoption rejected，功能保留为可选 search plugin |
| Repository gate | split full shards `2004/2004` passed，`31` skipped；latest M11/document/release focused `172/172` |
| Governance authority | `MASTER.md` revision 113 |

## 恢复入口

1. 普通任务先读本文件。
2. 根据 task ID 只展开 `MASTER.md` 对应章节。
3. 聊天迁移与组件结论分别读 conversation-ingestion policy 和 context-reliability assessment。
4. 当前代码或官方证据与历史 memory 冲突时，以当前证据为准。
5. 需要历史完成证据时按 `docs/reports/verified-state.md` 的 task/evidence ref 有界展开。
6. 压缩、Skill 选择、文档更新和 supersedes 走对应 event/lifecycle contract；恢复只展开当前任务的最小引用。
