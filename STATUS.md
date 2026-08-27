# Continuity Plane Status

版本：revision 97  
日期：2026-08-28  
canonical plan：`MASTER.md`

## 当前状态

| 字段 | 值 |
|---|---|
| 当前 Campaign | M10 跨项目发布 |
| active work | M10-01：跨项目 shadow pilot（🟡） |
| next action | 按 exact match key 采集 Platform、ProjectCompute 等至少两个项目/协作 profile 的 matched live A/B，每臂至少 `3` 段；M10-12 修复在下一预发行版发布 |
| hard blocker | PyPI Trusted Publisher 尚未绑定；matched live A/B 每臂仍未达到 `3` 段；Foundation Account 尚未初始化 Continuity 状态 |
| repository mode | internal development repository + fresh-history public mirror |
| production state | GitHub/PyPI alpha.7 published；shared pilot planned |

## 已验证摘要

| 对象 | 状态 |
|---|---|
| Evidence index | [`docs/reports/verified-state.md`](docs/reports/verified-state.md) |
| Verified campaigns | M0-M8 core 与 M9-01..07 见 Evidence index；M9-08..11 planned |
| alpha.7 publication | GitHub `v0.1.0-alpha.7` 与 PyPI `continuity-plane==0.1.0a7` 已发布；wheel/sdist SHA-256 与 GitHub `SHA256SUMS` 一致；从 PyPI 与 GitHub asset 的独立安装、init、verify、doctor 通过 |
| alpha.1 → alpha.7 audit | immutable tag tree 差异 `28` files（`16` added、`12` modified、`+10,282/-94`）；tag source 对应 `27` 个开发提交；公开 release history 为脱敏 projection，功能差异以标签树、artifact 和测试为准 |
| M10-11 foundation | CLI `0.1.0a7` + plugin candidate `0.1.0-alpha.7+codex.20260827062834`；current-only STATUS、Git common-dir、source refresh、dependency return、idle delivery activation 和 exact effect scopes 已实现；matched improvement remains open |
| post-tag boundary | downstream source-control local prerequisite、只读 forge/release 查询和同 Session 连续 effect intent 三项修复已在开发分支验证，明确标为 unreleased |
| M10-12 control-plane fix | `continuity autorun`、MCP retry、source-evidence activation rebind 与 provider/host/repository/worktree/branch intent isolation 已通过 `61` 项聚焦测试；N-69-08 snapshot MCP continued/already-continued，原 Platform revision/event `202/202`、N-69-08、Windows blocker 保持不变 |
| M10-12 completion | M10-12 已通过 checkpoint-bound autorun、source evidence activation、跨仓 intent isolation、N-69-08 snapshot 和 `61/61` focused tests；当前公开 alpha.7 不含该修复，需下一预发行版验收 |
| Coordination/views | shared Work、forge、notification、Project Graph、2,000-node Impact gates passed；authority violation `0` |
| Release baseline | Linux/macOS/Windows native matrix `18/18`；local state bundle export/import/rollback available；public privacy scan `0` findings |
| Documentation audit | README、USAGE、CHANGELOG、public docs、templates 与 policy 全文审阅；公开 Markdown 内部阶段标记 `0`，本地链接缺失 `0`，外部链接 `8/8` 可达；repository verification passed |
| Governance authority | `MASTER.md` revision 97 |

## 恢复入口

1. 普通任务先读本文件。
2. 根据 task ID 只展开 `MASTER.md` 对应章节。
3. 聊天迁移与组件结论分别读 conversation-ingestion policy 和 context-reliability assessment。
4. 当前代码或官方证据与历史 memory 冲突时，以当前证据为准。
5. 需要历史完成证据时按 `docs/reports/verified-state.md` 的 task/evidence ref 有界展开。
6. 压缩、Skill 选择、文档更新和 supersedes 走对应 event/lifecycle contract；恢复只展开当前任务的最小引用。
