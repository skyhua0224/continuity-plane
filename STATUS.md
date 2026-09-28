# Continuity Plane Status

版本：revision 135  
日期：2026-09-28  
canonical plan：`MASTER.md`

## 当前安全姿态与收口目标

| 字段 | 值 |
|---|---|
| integration posture | alpha12 marketplace runtime active；doctor 已解析 6/6 hook 命令、真实脚本入口与包版本；6/6 trusted；不调用会重写整份配置的 `cc-switch config common set` |
| 业务影响 | 已停止配置写入；业务代码与 State 未修改；provider 并发变化未归因 |
| 收口目标 | M11-00：Zero-friction continuity core |
| 收口条件 | 三项目各 `3` 段 A/B；复活、重复、误阻断为 `0`；恢复与 history token 达标 |
| 未达标策略 | 保持本机非阻断；不启用公共默认 |

## 当前状态

| 字段 | 值 |
|---|---|
| 当前 Campaign | M11 零阻断收口 |
| active work | M11-00：真实插件采用与旧线程原位恢复（🟡） |
| next action | 用修订后的候选执行显式 guarded install 后，完成宿主原生 compaction 回归与三项目 matched A/B；不迁移聊天、不切换 provider |
| hard blocker | 持续检索采用与 token 收益未达标；暂不发版 |
| repository mode | internal development repository + fresh-history public mirror |
| production state | alpha12 公开；本机 marketplace core 候选 `+codex.20260909124804`，未发 alpha13 |

## 已验证摘要

| 对象 | 状态 |
|---|---|
| Evidence index | [`docs/reports/verified-state.md`](docs/reports/verified-state.md) |
| Verified campaigns | M0-M8 core 与 M9-01..07 见 Evidence index；M9-08..11 planned |
| Publication | 历史发行与验收见 CHANGELOG |
| Release baseline | 历史跨平台与视图验收见 Evidence index |
| Documentation audit | 双语文档区分公开版与本机候选 |
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
| Native probe | 前后 hook 执行、marker 保留；lookup `1/0`，采用门未过；实报窗口 828400；[receipt](experiments/evidence/m11-00-installed-hook-native.json) |
| Safe installation | [历史收据](experiments/evidence/m11-00-safe-codex-install.json)仅证明安装时校验；不证明当前任务可见或持续启用 |
| M11 retrieval fallback | 本地候选：空结果成功返回，原回执哈希/字节合同保持；不可用或零命中不触发安装；未部署到业务任务 |
| M11 live-command continuation | advisory PostToolUse 识别仍运行的 shell handle，仅注入一次 `write_stdin` 轮询提示；State/claim/命令门均为 `0`；完成命令无提示；复现与验证见 [receipt](experiments/evidence/m11-03-live-command-handle.json) |
| M11 safe plugin replacement | 本机候选 `0.1.0-alpha.12+codex.20260909122445`；受保护 provider/model/auth/proxy 指纹 `70` 条保持不变；Codex 原生发现 6/6 plugin hooks trusted/enabled；无业务 State 写入；[install receipt](experiments/evidence/m11-00-safe-codex-install-replace.json)、[preflight](experiments/evidence/m11-00-replaced-hook-preflight.json) |
| M11 hook continuation contract | compact SessionStart 注入静默 next_action 合同；PostCompact 仅校验 canary；每个 turn 至多一次 bounded-read 提示和长命令 next-action 提示；不阻断、不调用 State；Git worktree resume 解析到注册治理根；[install receipt](experiments/evidence/m11-00-safe-codex-install-hooks-v2.json)、[preflight](experiments/evidence/m11-00-installed-hook-preflight-v2.json) |
| M11 runtime diagnosis | doctor 解析 marketplace 实际 plugin，检测 no-op stub、入口缺失、hook 命令缺口、版本漂移、active claim 80% lease 预警与 resume packet 体积；明确执行意图不再被 cursor 降级为只答问题；PostCompact 恢复为静默 canary，不重复注入；lifecycle/release/autorun focused `84/84` 与新增 doctor 回归通过 |
| Governance authority | `MASTER.md` revision 135 |

## 恢复入口

1. 普通任务先读本文件。
2. 根据 task ID 只展开 `MASTER.md` 对应章节。
3. 聊天迁移与组件结论分别读 conversation-ingestion policy 和 context-reliability assessment。
4. 当前代码或官方证据与历史 memory 冲突时，以当前证据为准。
5. 需要历史完成证据时按 `docs/reports/verified-state.md` 的 task/evidence ref 有界展开。
6. 压缩、Skill 选择、文档更新和 supersedes 走对应 event/lifecycle contract；恢复只展开当前任务的最小引用。
