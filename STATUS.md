# Continuity Plane Status

版本：revision 101  
日期：2026-08-29  
canonical plan：`MASTER.md`

## 当前状态

| 字段 | 值 |
|---|---|
| 当前 Campaign | M10 跨项目发布 |
| active work | M10-01：跨项目 shadow pilot（🟡） |
| next action | 完成 M10-17 全量回归、公开构建与多根只读验收；随后采集两个 profile 的 matched live A/B，每臂 `3` 段 |
| hard blocker | PyPI Trusted Publisher 未绑定；matched A/B 未达 `3` 段；M10-17 待发布 |
| repository mode | internal development repository + fresh-history public mirror |
| production state | GitHub/PyPI alpha.8 published；shared pilot planned |

## 已验证摘要

| 对象 | 状态 |
|---|---|
| Evidence index | [`docs/reports/verified-state.md`](docs/reports/verified-state.md) |
| Verified campaigns | M0-M8 core 与 M9-01..07 见 Evidence index；M9-08..11 planned |
| alpha.8 publication | GitHub `v0.1.0-alpha.8` 与 PyPI `continuity-plane==0.1.0a8` 已发布；wheel/sdist SHA-256 与 GitHub `SHA256SUMS` 一致；核心包、Codex plugin 和独立安装、init、verify、doctor 通过 |
| alpha.1 → alpha.7 audit | immutable tag tree 差异 `28` files（`16` added、`12` modified、`+10,282/-94`）；tag source 对应 `27` 个开发提交；公开 release history 为脱敏 projection，功能差异以标签树、artifact 和测试为准 |
| M10-11 foundation | CLI `0.1.0a8` + plugin `0.1.0-alpha.8`；current-only STATUS、Git common-dir、source refresh、dependency return、idle delivery activation 和 exact effect scopes 已实现；matched improvement remains open |
| M10-12 completion | M10-12 已通过 checkpoint-bound autorun、source evidence activation、跨仓 intent isolation、脱敏 snapshot 和 `61/61` focused tests；修复已包含在 alpha.8 |
| M10-14 Session binding | plugin candidate `0.1.0-alpha.8+codex.20260828175545`；首次显式 resume 优先于进程 cwd，跨根重绑在 CLI 前拒绝；focused `48/48`、full suite `1973` passed、真实双项目只读验收通过；公开发行待下一版本 |
| M10-15 external workspace | plugin candidate `0.1.0-alpha.8+codex.20260829043730`；治理根可注册独立 delivery workspace；local/history-rewrite effect、repo scope、repository digest 和 workdir intent 已分离；focused `71/71`、full suite `1975` passed；真实注册 State/Event 保持 `75/75`；公开发行待下一版本 |
| M10-17 multi-project Session | 本地候选已支持一个 Session 显式绑定多个项目根并按 `continuity_resume(root=...)` 切换；cwd 不覆盖 active root，未绑定或损坏 binding 在 CLI 前拒绝；schema registry hash 已登记；focused `46/46` 通过；完整回归、实际双项目只读验收和 alpha.9 发布待完成 |
| Coordination/views | shared Work、forge、notification、Project Graph、2,000-node Impact gates passed；authority violation `0` |
| Release baseline | Linux/macOS/Windows native matrix `18/18`；local state bundle export/import/rollback available；public privacy scan `0` findings |
| Documentation audit | README、USAGE、CHANGELOG、public docs、templates 与 policy 全文审阅；公开 Markdown 内部阶段标记 `0`，本地链接缺失 `0`，外部链接 `8/8` 可达；repository verification passed |
| Governance authority | `MASTER.md` revision 101 |

## 恢复入口

1. 普通任务先读本文件。
2. 根据 task ID 只展开 `MASTER.md` 对应章节。
3. 聊天迁移与组件结论分别读 conversation-ingestion policy 和 context-reliability assessment。
4. 当前代码或官方证据与历史 memory 冲突时，以当前证据为准。
5. 需要历史完成证据时按 `docs/reports/verified-state.md` 的 task/evidence ref 有界展开。
6. 压缩、Skill 选择、文档更新和 supersedes 走对应 event/lifecycle contract；恢复只展开当前任务的最小引用。
