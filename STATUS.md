# Continuity Plane Status

版本：revision 93  
日期：2026-08-26  
canonical plan：`MASTER.md`

## 当前状态

| 字段 | 值 |
|---|---|
| 当前 Campaign | M10 跨项目发布 |
| active work | M10-01：AlkaidLab 三仓 shadow pilot（🟡） |
| next action | Platform 业务 Session 用 alpha.7 stale-source heartbeat 自助恢复 N-69-08；随后验收新 Session 自动绑定、effect preflight 与真实 compaction candidate，形成 matched segment 1/3 |
| hard blocker | Platform N-69-14 claim 已过期；两个项目 matched candidate 均未达到 `3` 段；Claude live provider 403；Platform Windows 物理路径双向零 ALTP packet；Platform flat v1 Work 缺 canonical DAG |
| repository mode | internal development repository + fresh-history public mirror |
| production state | alpha published；shared pilot planned |

## 已验证摘要

| 对象 | 状态 |
|---|---|
| Evidence index | [`docs/reports/verified-state.md`](docs/reports/verified-state.md) |
| Verified campaigns | M0-M8 core 与 M9-01..07 见 Evidence index；M9-08..11 planned |
| M10-00/01 pilots | self M10-01 active；Platform 当前 State/projection `162/162`、Work N-69-14、checkpoint/source valid、lease expired、`read_only=true`；ProjectCompute nested return `78 -> 79 -> 80` 由业务 Session 自主完成，未修改 SQLite |
| M10-11 foundation | CLI `0.1.0a7` + plugin candidate `0.1.0-alpha.7+codex.20260826175851`；resume 自动修复 current STATUS；stale-source owner heartbeat 在单一 Event 重绑 current canonical source evidence、续租并刷新 checkpoint；nested return、delivery contract、exact effect scopes、Git common-dir 与 startup attestation 已实现；matched improvement remains open |
| ProjectCompute pilot | 主目录无独立 `.continuity`，通过 Git common-dir 绑定 execution root；State/projection `80/80`、active issue-773、claim/checkpoint/source/lease valid；950K baseline 保留；alpha.7 live effect/compaction candidate 待采集 |
| Coordination/views | shared Work、forge、notification、Project Graph、2,000-node Impact gates passed；authority violation `0` |
| Release baseline | GitHub/PyPI alpha published；Linux/macOS/Windows native matrix `18/18`；public privacy scan `0` findings |
| Repository verification | alpha.7 full discovery `1,951` run、failure/error `0`、conditional skip `31`、`406.527 s`；targeted Ruff、schema registry、plugin validator、wheel install 与 two-project resume pass |
| Governance authority | `MASTER.md` revision 93 |

## 恢复入口

1. 普通任务先读本文件。
2. 根据 task ID 只展开 `MASTER.md` 对应章节。
3. 聊天迁移与组件结论分别读 conversation-ingestion policy 和 context-reliability assessment。
5. 当前代码或官方证据与历史 memory 冲突时，以当前证据为准。
6. 需要历史完成证据时按 `docs/reports/verified-state.md` 的 task/evidence ref 有界展开。
7. 压缩、Skill 选择、文档更新和 supersedes 走对应 event/lifecycle contract；恢复只展开当前任务的最小引用。
