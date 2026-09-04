# Continuity Plane Status

版本：revision 120  
日期：2026-09-04  
canonical plan：`MASTER.md`

## 当前安全姿态与收口目标

| 字段 | 值 |
|---|---|
| integration posture | `auto` 非阻断；三插件启用；hooks `3/3`；命令门 0 |
| 业务影响 | 三个业务项目不受门禁影响 |
| 收口目标 | M11-00：Zero-friction continuity core |
| 收口条件 | 三项目各 `3` 段 A/B；复活、重复、误阻断为 `0`；恢复与 history token 达标 |
| 未达标策略 | 保持本机非阻断；不启用公共默认 |

## 当前状态

| 字段 | 值 |
|---|---|
| 当前 Campaign | M11 零阻断收口 |
| active work | M11-00：真实插件采用与旧线程原位恢复（🟡） |
| next action | 三项目回执；大型仓库索引 A/B |
| hard blocker | Platform 与 ProjectCompute input-token gate 均未达 `30%` |
| repository mode | internal development repository + fresh-history public mirror |
| production state | alpha.10 已发布；本机 candidate `20260904045120` 已安装；索引候选已验证；公共默认待 A/B |

## 已验证摘要

| 对象 | 状态 |
|---|---|
| Evidence index | [`docs/reports/verified-state.md`](docs/reports/verified-state.md) |
| Verified campaigns | M0-M8 core 与 M9-01..07 见 Evidence index；M9-08..11 planned |
| alpha.10 publication | GitHub/PyPI、42-commit public history、141-file mirror、wheel/sdist/3-plugin zip/`SHA256SUMS` 和 CI 通过 |
| Coordination/views | shared Work、forge、notification、Project Graph、2,000-node Impact gates passed；authority violation `0` |
| Release baseline | Linux/macOS/Windows native matrix `18/18`；local state bundle export/import/rollback available；public privacy scan `0` findings |
| Documentation audit | README、USAGE、CHANGELOG、public docs、templates 与 policy 全文审阅；公开 Markdown 内部阶段标记 `0`，本地链接缺失 `0`，外部链接 `8/8` 可达；repository verification passed |
| M11 adoption | doctor active；hooks `3/3`；inspect 后读取 `5 -> 0`、input/output `-58.10%/-67.26%`、写入 `0`；[evidence](experiments/evidence/m11-00-codex-adoption-probe.json) |
| M11 hook efficiency | `40` 组：calls `-33.33%`；context `-83.73%`；stdout `-85.18%`；deny/stop `0/0`；[evidence](experiments/evidence/m11-00-codex-hook-efficiency.json) |
| M11 Platform A/B | `3+3`：input/output/tool `-12.14%/-28.84%/-41.46%`；veto `0`；未过 input gate |
| M11 ProjectCompute A/B | `3+3`：input/output/tool `-2.36%/-13.70%/+2.62%`；veto `0`；未过 input gate |
| M11 bounded retrieval | current tracked worktree、revision/hash、完整 JSON budget 与 CLI `3/3` |
| M11 code index | 两仓库实测二次重算 `0`、缓存隔离；[evidence](experiments/evidence/m11-02-code-index-real.json) |
| Repository gate | full `2014/2014` passed，`31` skipped；release `76/76`、governance `49/49`、public `5/5` |
| Governance authority | `MASTER.md` revision 120 |

## 恢复入口

1. 普通任务先读本文件。
2. 根据 task ID 只展开 `MASTER.md` 对应章节。
3. 聊天迁移与组件结论分别读 conversation-ingestion policy 和 context-reliability assessment。
4. 当前代码或官方证据与历史 memory 冲突时，以当前证据为准。
5. 需要历史完成证据时按 `docs/reports/verified-state.md` 的 task/evidence ref 有界展开。
6. 压缩、Skill 选择、文档更新和 supersedes 走对应 event/lifecycle contract；恢复只展开当前任务的最小引用。
