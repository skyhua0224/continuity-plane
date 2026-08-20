# Continuity Plane Status

版本：revision 84  
日期：2026-08-20  
canonical plan：`MASTER.md`

## 当前状态

| 字段 | 值 |
|---|---|
| 当前 Campaign | M10 跨项目发布 |
| active work | M10-01：AlkaidLab 三仓 shadow pilot（🟡） |
| next action | M10-11：在新 Codex App Session 收集 native hook candidate；恢复 Claude live provider 后执行 matched `3+3` study |
| hard blocker | 当前 App Session 未热加载新 plugin；Codex `exec` 不装载 personal plugin；Claude live provider 403；Platform flat v1 Work 缺 canonical DAG |
| repository mode | internal development repository + fresh-history public mirror |
| production state | alpha published；shared pilot planned |

## 已验证摘要

| 对象 | 状态 |
|---|---|
| Evidence index | [`docs/reports/verified-state.md`](docs/reports/verified-state.md) |
| Verified campaigns | M0-M8 core 与 M9-01..07 见 Evidence index；M9-08..11 planned |
| M10-00/01 pilots | self M10-01；Platform N-69-09 active at `36/36`，旧 claim 已 reclaim 为 `claim-n-69-09-reclaimed-2`，checkpoint verified |
| Local lifecycle adapter | Recovery Envelope + Pre/PostCompact canary + cursor + 8-rule Skill lock；MCP root/actor/claim binding verified |
| Platform 20h probe | restore `5/5`；recovery narration `5/5`；post-compact input `54.8-56.8K`；matched gate open；heartbeat identity 修复已部署 |
| Effective context | configured 1M catalog 的 provider-reported window `950,000`；历史 700K compact `783,628 -> 24,776`，next input `59,172`；新裸 Codex baseline `59,177`；matched candidate unavailable |
| Claude baseline | `96` deduped messages；auto compact `1,001,838 -> 13,041`；duration `171,480 ms`；live candidate 403/unavailable |
| M10-11 foundation | `7` strict schemas；local Recovery Envelope `3,037 B`；checkpoint/cursor/Skill lock pass；raw text `0`；cross-project/cross-provider improvement claim `false` |
| Release baseline | GitHub/PyPI alpha published；Linux/macOS/Windows native matrix `18/18`；public privacy scan `0` findings |
| Repository verification | `1,900` discovered；`1,869` pass；`31` conditional skip；`0` fail；`433.160 s` |
| Governance authority | `MASTER.md` revision 84 |

## 恢复入口

1. 普通任务先读本文件。
2. 根据 task ID 只展开 `MASTER.md` 对应章节。
3. 聊天迁移与组件结论分别读 conversation-ingestion policy 和 context-reliability assessment。
5. 当前代码或官方证据与历史 memory 冲突时，以当前证据为准。
6. 需要历史完成证据时按 `docs/reports/verified-state.md` 的 task/evidence ref 有界展开。
7. 压缩、Skill 选择、文档更新和 supersedes 走对应 event/lifecycle contract；恢复只展开当前任务的最小引用。
