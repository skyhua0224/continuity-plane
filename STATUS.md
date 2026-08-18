# Continuity Plane Status

版本：revision 79  
日期：2026-08-18  
canonical plan：`MASTER.md`

## 当前状态

| 字段 | 值 |
|---|---|
| 当前 Campaign | M10 跨项目发布 |
| active work | M10-09：native release matrix（🟡） |
| next action | 实现 canonical MASTER importer/projection 与 export/import/rollback CLI，使用本仓和 Platform 双 fixture 验收 |
| hard blocker | 三端 install/verify/uninstall 已通过；M10-09 仍缺 importer 和跨 adapter 迁移/回滚 |
| repository mode | internal development repository + fresh-history public mirror |
| production state | Continuity Plane local-embedded alpha published；shared production pilot planned |

## 已验证摘要

| 对象 | 状态 |
|---|---|
| Evidence index | [`docs/reports/verified-state.md`](docs/reports/verified-state.md) |
| Documentation lifecycle | `140` documents；STATUS reduction `75.3022%`；recovery `5/5`；validator p95 `<100 ms`；exact receipt governed |
| Git admission | M0-11 offline contract `26/26`；repository verifier 通过 |
| M3/M4 | routing、Idea continuity、Skill resolver/quarantine verified；details in Evidence index |
| M5/M6/M7 | M5-01..08、M6 retrieval/recall、M7-01..06 verified；量化 receipts 与 provenance 见 Evidence index |
| M8 | durability、authorization、adaptation、forge、unattended dispatch 与 notifications verified；live Temporal conditional |
| M9 | projection/governance/vault/impact M9-01..07 verified；graphical product M9-08..11 planned |
| M10-00/01 pilots | self campaign `passed`；Platform 真实 shadow 已初始化，typed revision `0`，promoted objects `0`；importer gap open |
| M10-08 release surface | current compiler tree `115` files；public `5/5`；packaged module graph `43` importable；fresh-history candidate `28` commits；worktree/history/wheel/sdist leak `0` |
| M10-09 native matrix | receipt `6/6`；Linux `870.4542/96.9552/0.4078 ms`；SkyServer macOS `6385.1462/155.7239/24.8827 ms`；Skyindows Windows `10078.0/156.0/47.0 ms`；export/import/rollback `blocked` |
| M10-10 publication | GitHub release `v0.1.0-alpha.1`；PyPI `continuity-plane==0.1.0a1`；`28` commits；contributor `skyhua0224`；About/topics/assets verified |
| Codex 1M A/B | real provider `3+3+3`；baseline `32,857`、packet `19,633`、large `397,631` input tokens；packet `-40.2471%`；quality `100%`；Skill warning `9/9` |
| Harness ablation | real provider `4 arms x 3`；bare `19,608`、State `19,649`、full `19,748`、history `32,854`；quality `100%`；full-vs-bare `+0.7140%` |
| Skill overlay | source bytes `501,543 -> 17,349` (`-96.5409%`)；provider input `19,593 -> 18,469` (`-5.7367%`)；warnings `3/3 -> 0/3` |
| Real code retrieval | input `-50.0153%`；tool calls `-57.8947%`；wall time `-27.4120%`；quality `100%` |
| Effective 1M | 700K compact `783,628 -> 24,776`；next packet `59,172`；recovery `100%`；M10-11 window/token longitudinal result unavailable |
| Repository verification | `1824` discovered；`1793` pass；`31` conditional skip；`0` fail；`229.393 s`；verifier passed |
| Governance authority | `MASTER.md` revision 79 |

## 恢复入口

1. 普通任务先读本文件。
2. 根据 task ID 只展开 `MASTER.md` 对应章节。
3. 聊天迁移先读 `docs/policies/conversation-ingestion.md`。
4. 组件选择和实验结论读 `docs/research/context-reliability-assessment-2026-08-09.md`。
5. 当前代码或官方证据与历史 memory 冲突时，以当前证据为准。
6. 需要历史完成证据时按 `docs/reports/verified-state.md` 的 task/evidence ref 有界展开。
7. 压缩、Skill 选择或 MASTER revision 后按 `context.dogfood-event/v1alpha1` 追加受控观察事件；仅在 schema/hash 变化或 validator 失败时重读完整 policy。
8. 文档更新、拆分和 supersedes 按 `docs/policies/documentation-lifecycle.md`，恢复时只展开当前任务对应的最小合同。
