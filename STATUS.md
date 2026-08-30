# Continuity Plane Status

版本：revision 108  
日期：2026-08-30  
canonical plan：`MASTER.md`

## 当前安全姿态与收口目标

| 字段 | 值 |
|---|---|
| integration posture | `observe` design；本机 plugin disabled |
| 业务影响 | Platform、ProjectCompute、Foundation Account 不受命令门禁影响 |
| 收口目标 | M11-00：Zero-friction continuity core |
| 收口条件 | 三项目各 `3` 段 matched baseline/candidate；旧 Work 复活、重复回答、误阻断为 `0`；恢复读取和 history-heavy token 达到目标降幅 |
| 未达标策略 | 保持 disabled，不发布新的默认启用版本 |

## 当前状态

| 字段 | 值 |
|---|---|
| 当前 Campaign | M10 跨项目发布 |
| active work | M10-01：跨项目 shadow pilot（🟡） |
| next action | 采集两个 profile 的 matched live A/B，每臂 `3` 段 |
| hard blocker | matched A/B 未达 `3` 段 |
| repository mode | internal development repository + fresh-history public mirror |
| production state | GitHub/PyPI alpha.9 prerelease published；shared pilot planned |

## 已验证摘要

| 对象 | 状态 |
|---|---|
| Evidence index | [`docs/reports/verified-state.md`](docs/reports/verified-state.md) |
| Verified campaigns | M0-M8 core 与 M9-01..07 见 Evidence index；M9-08..11 planned |
| alpha.9 publication | GitHub prerelease、PyPI、脱敏镜像、wheel/sdist、`SHA256SUMS`、CI 和本地 CLI/plugin 已通过 |
| alpha.1 → alpha.7 audit | immutable tag tree 差异 `28` files（`16` added、`12` modified、`+10,282/-94`）；tag source 对应 `27` 个开发提交；公开 release history 为脱敏 projection，功能差异以标签树、artifact 和测试为准 |
| M10-11..15 | autorun、source rebind、effect isolation、Session binding 与 external workspace 已通过各自 focused/full tests；matched improvement remains open |
| M10-17 multi-project Session | 显式 `continuity_resume(root=...)` 切换多个项目；cwd 不覆盖身份；binding digest/legacy/fail-closed 通过；focused `46/46`、full `1975/1975`（`31` skipped）；GitHub/PyPI alpha.9 已发布 |
| Coordination/views | shared Work、forge、notification、Project Graph、2,000-node Impact gates passed；authority violation `0` |
| Release baseline | Linux/macOS/Windows native matrix `18/18`；local state bundle export/import/rollback available；public privacy scan `0` findings |
| Documentation audit | README、USAGE、CHANGELOG、public docs、templates 与 policy 全文审阅；公开 Markdown 内部阶段标记 `0`，本地链接缺失 `0`，外部链接 `8/8` 可达；repository verification passed |
| M11-00 zero-friction reset | plugin disabled；intent-aware startup、non-blocking recovery 与 single-path compact continuation 通过 `59/59` focused tests；[pre-reset baseline](experiments/evidence/m11-00-live-baseline-20260830.json) |
| M11 hook efficiency | `40` 组：calls `-33.33%`，model context `-32.45%`，stdout `-48.49%`，wall p50/p95 `-12.33%/-12.63%`，deny/stop `0/0`；[receipt](experiments/evidence/m11-00-codex-hook-efficiency.json) |
| Repository gate | full `1995/1995` passed，`31` skipped；治理 revision、149-document manifest 与五份派生 benchmark receipt 已重建并通过 current-provenance gate |
| Governance authority | `MASTER.md` revision 108 |

## 恢复入口

1. 普通任务先读本文件。
2. 根据 task ID 只展开 `MASTER.md` 对应章节。
3. 聊天迁移与组件结论分别读 conversation-ingestion policy 和 context-reliability assessment。
4. 当前代码或官方证据与历史 memory 冲突时，以当前证据为准。
5. 需要历史完成证据时按 `docs/reports/verified-state.md` 的 task/evidence ref 有界展开。
6. 压缩、Skill 选择、文档更新和 supersedes 走对应 event/lifecycle contract；恢复只展开当前任务的最小引用。
