# Continuity Plane Status

版本：revision 137  
日期：2026-09-28  
canonical plan：`MASTER.md`

## 当前安全姿态与收口目标

| 字段 | 值 |
|---|---|
| integration posture | 本机 PyPI CLI `0.1.0a13`；marketplace 候选 `+codex.20260928091906` active；doctor 已解析 6/6 hook 命令、真实脚本入口与包版本；6/6 trusted；opencodex 保护面不变 |
| 业务影响 | 已停止配置写入；业务代码与 State 未修改；provider 并发变化未归因 |
| 收口目标 | M11-00：Zero-friction continuity core |
| 收口条件 | 三项目各 `3` 段 A/B；复活、重复、误阻断为 `0`；恢复与 history token 达标 |
| 未达标策略 | 保持本机非阻断；不启用公共默认 |

## 当前状态

| 字段 | 值 |
|---|---|
| 当前 Campaign | M11 零阻断收口 |
| active work | M11-00：真实插件采用与旧线程原位恢复（🟡） |
| next action | 在真实业务任务中验证 alpha.13 adoption 与 matched A/B；不迁移聊天、不切换 provider、不重复安装 |
| hard blocker | 持续检索采用与 token 收益未达标；alpha.13 只作为非阻断候选发布，不宣称普遍 token 节省 |
| repository mode | internal development repository + fresh-history public mirror |
| production state | alpha13 已发布到 GitHub Release 与 PyPI；GitHub main/tag CI 和 Publish workflow 均通过 |

## 已验证摘要

| 对象 | 状态 |
|---|---|
| Evidence index | [`docs/reports/verified-state.md`](docs/reports/verified-state.md) |
| Publication | 历史发行与验收见 CHANGELOG |
| Native probe | hook 前后执行、marker 保留；lookup `1/0`，采用门未过；[receipt](experiments/evidence/m11-00-installed-hook-native.json) |
| Runtime diagnosis | doctor 校验真实 plugin、6/6 hook、版本、lease 预警与 packet 体积；明确执行意图保持 continuation |
| opencodex-safe install | [install v5](experiments/evidence/m11-00-safe-codex-install-hooks-v5.json)、[preflight v4](experiments/evidence/m11-00-installed-hook-preflight-v4.json)；保护面未变 |
| alpha.13 publication | GitHub main/tag CI、release workflow、PyPI workflow 均通过；发行说明见 [alpha.13](docs/releases/0.1.0-alpha.13.md)；本机原生 compaction hook 全部执行且 marker 保留，但 lookup `0`，adoption gate 未通过 |
| Governance authority | `MASTER.md` revision 137 |

## 恢复入口

1. 普通任务先读本文件。
2. 根据 task ID 只展开 `MASTER.md` 对应章节。
3. 聊天迁移与组件结论分别读 conversation-ingestion policy 和 context-reliability assessment。
4. 当前代码或官方证据与历史 memory 冲突时，以当前证据为准。
5. 需要历史完成证据时按 `docs/reports/verified-state.md` 的 task/evidence ref 有界展开。
6. 压缩、Skill 选择、文档更新和 supersedes 走对应 event/lifecycle contract；恢复只展开当前任务的最小引用。
