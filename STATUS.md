# Continuity Plane Status

版本：revision 129  
日期：2026-09-05  
canonical plan：`MASTER.md`

## 当前安全姿态与收口目标

| 字段 | 值 |
|---|---|
| integration posture | `auto`；advisory candidate 未安装到业务 Session |
| 业务影响 | 本轮未修改业务仓库、State 或 Codex 配置 |
| 收口目标 | M11-00：Zero-friction continuity core |
| 收口条件 | 三项目各 `3` 段 A/B；复活、重复、误阻断为 `0`；恢复与 history token 达标 |
| 未达标策略 | 保持本机非阻断；不启用公共默认 |

## 当前状态

| 字段 | 值 |
|---|---|
| 当前 Campaign | M11 零阻断收口 |
| active work | M11-00：真实插件采用与旧线程原位恢复（🟡） |
| next action | 确认宿主项目/hook 信任；再做候选原生压缩、三项目 A/B |
| hard blocker | 宿主未装载候选 hook；原生验收失败，禁止据此发版 |
| repository mode | internal development repository + fresh-history public mirror |
| production state | alpha12 已发布；本轮改动未发布、未安装 |

## 已验证摘要

| 对象 | 状态 |
|---|---|
| Evidence index | [`docs/reports/verified-state.md`](docs/reports/verified-state.md) |
| Verified campaigns | M0-M8 core 与 M9-01..07 见 Evidence index；M9-08..11 planned |
| Publication | 历史发行与验收见 CHANGELOG |
| Coordination/views | shared Work、forge、notification、Project Graph、2,000-node Impact gates passed；authority violation `0` |
| Release baseline | Linux/macOS/Windows native matrix `18/18`；local state bundle export/import/rollback available；public privacy scan `0` findings |
| Documentation audit | 双语文档区分未发布改动；模型/key/config 未改 |
| M11 adoption | 历史基线见 [evidence](experiments/evidence/m11-00-codex-adoption-probe.json)；不代表候选当前已采用 |
| M11 hook efficiency | `40` 组：calls `-33.33%`；context `-83.73%`；stdout `-85.18%`；deny/stop `0/0`；[evidence](experiments/evidence/m11-00-codex-hook-efficiency.json) |
| M11 Platform A/B | `3+3`：input/output/tool `-12.14%/-28.84%/-41.46%`；veto `0`；未过 input gate |
| M11 ProjectCompute A/B | `3+3`：input/output/tool `-2.36%/-13.70%/+2.62%`；veto `0`；未过 input gate |
| M11 bounded retrieval | current tracked worktree、revision/hash、完整 JSON budget 与 CLI `3/3` |
| M11 code index | 两仓库实测二次重算 `0`、缓存隔离；[evidence](experiments/evidence/m11-02-code-index-real.json) |
| M11 lookup routing | 单工具 search MCP + CLI/API；State MCP 不承载检索；source stale 继续普通开发；默认采用 A/B pending |
| M11 State-only | genesis `1/1`、proposal/checkpoint、可重试；State-only MCP/STATUS；stale Work 不注入；`134/134` |
| M11 observability | bounded policy/retention/lock/cache/report `45/45`；不记录 transcript/source/tool body |
| M11 advisory fixture | 真实 CLI、多进程、零 State 修改；[receipt](experiments/evidence/m11-00-advisory-hook-real-cli.json)；非宿主原生压缩 |
| Repository gate | full `2117`、skip `31`；已知失败组修复；候选审查 5 个负向复现通过 |
| Native probe | 真实压缩已发生、marker 保留；hook/lookup 为 0；配置 1M、实报 828400；[receipt](experiments/evidence/m11-00-native-hook-review.json)；未通过 |
| Governance authority | `MASTER.md` revision 129 |

## 恢复入口

1. 普通任务先读本文件。
2. 根据 task ID 只展开 `MASTER.md` 对应章节。
3. 聊天迁移与组件结论分别读 conversation-ingestion policy 和 context-reliability assessment。
4. 当前代码或官方证据与历史 memory 冲突时，以当前证据为准。
5. 需要历史完成证据时按 `docs/reports/verified-state.md` 的 task/evidence ref 有界展开。
6. 压缩、Skill 选择、文档更新和 supersedes 走对应 event/lifecycle contract；恢复只展开当前任务的最小引用。
