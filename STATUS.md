# Continuity Plane Status

版本：revision 90  
日期：2026-08-23  
canonical plan：`MASTER.md`

## 当前状态

| 字段 | 值 |
|---|---|
| 当前 Campaign | M10 跨项目发布 |
| active work | M10-01：AlkaidLab 三仓 shadow pilot（🟡） |
| next action | Platform task 热重载 alpha.6 MCP，在 revision 111 对 N-69-08 执行同 Session expired-claim reclaim；成功后继续原 Work，不由控制面接手产品开发 |
| hard blocker | Platform N-69-08 alpha.6 live reclaim 待执行；matched compaction 尚未发生；Claude live provider 403；Codex arbitrary shell effect 尚无 active-claim 自动 preflight；Platform Windows 物理路径双向零 ALTP packet；Platform flat v1 Work 缺 canonical DAG |
| repository mode | internal development repository + fresh-history public mirror |
| production state | alpha published；shared pilot planned |

## 已验证摘要

| 对象 | 状态 |
|---|---|
| Evidence index | [`docs/reports/verified-state.md`](docs/reports/verified-state.md) |
| Verified campaigns | M0-M8 core 与 M9-01..07 见 Evidence index；M9-08..11 planned |
| M10-00/01 pilots | self M10-01 active；Platform idle activation `95/95 -> 96/96` live gate passed；latest reported revision `111`、Work N-69-08、claim `claim-n-69-08` lease expired；alpha.6 same-Session reclaim repair installed，live retry pending |
| M10-11 foundation | CLI `0.1.0a6` wheel `0080270f…` + plugin `0.1.0-alpha.6+codex.20260823220129` installed；read-only binding 仅对 source/checkpoint verified、lease expired、actor/claim 精确匹配的 reclaim 开放；candidate checkpoint 在 State CAS 前发布验证，失败保持 revision/Event/claim 不变；successor replay 幂等，其他写入 fail closed；matched improvement remains open |
| ProjectCompute pilot | 950K / `23.649 h` / `1.219B` input / `4` compactions baseline retained；CLI `0.1.0a4` active；无 claim 时发生 merge/install effect `2`，turn steer 后 revision/event `26/26`，delivery Work/claim/checkpoint/source/lease valid，`read_only=false`；既有 effect evidence 待 completion receipt |
| Coordination/views | shared Work、forge、notification、Project Graph、2,000-node Impact gates passed；authority violation `0` |
| Release baseline | GitHub/PyPI alpha published；Linux/macOS/Windows native matrix `18/18`；public privacy scan `0` findings |
| Repository verification | alpha.6 targeted MCP/recovery/activation/transition `57/57`；full discovery `1,934` run、failure/error `0`、conditional skip `31`、`348.445 s`；wheel/sdist build、repository gate 与 targeted Ruff pass |
| Governance authority | `MASTER.md` revision 90 |

## 恢复入口

1. 普通任务先读本文件。
2. 根据 task ID 只展开 `MASTER.md` 对应章节。
3. 聊天迁移与组件结论分别读 conversation-ingestion policy 和 context-reliability assessment。
5. 当前代码或官方证据与历史 memory 冲突时，以当前证据为准。
6. 需要历史完成证据时按 `docs/reports/verified-state.md` 的 task/evidence ref 有界展开。
7. 压缩、Skill 选择、文档更新和 supersedes 走对应 event/lifecycle contract；恢复只展开当前任务的最小引用。
