# Context Control Plane Status

版本：revision 61  
日期：2026-08-16  
canonical plan：`MASTER.md`

## 当前状态

| 字段 | 值 |
|---|---|
| 当前 Campaign | M8 多协作者与耐久执行 |
| active work | M8-05：权限、审计、tenant/project 隔离（🟡） |
| next action | 定义 actor/tenant/project authorization 与 audit Event 合同，先写越权拒绝和跨项目隔离失败测试 |
| hard blocker | 无；M3-01 的 Windows/macOS 原生 live fixture 保持 conditional |
| repository mode | research / shadow pilot |
| production state | local-embedded implemented / shadow pilot；shared production pilot planned |

## 已验证摘要

| 对象 | 状态 |
|---|---|
| Evidence index | [`docs/reports/verified-state.md`](docs/reports/verified-state.md) |
| Documentation lifecycle | M0-10 verified；STATUS reduction `75.1637%`；MASTER section reduction `68.9812%`；High/Medium review 0 |
| Git admission | M0-11 offline contract `26/26`；repository verifier 通过 |
| Skill adapters | M4-07/08/10 done；external=0 |
| Prior gates | M3-07/M3-08/M4-09 verified；replay and quarantine gates pass；details in evidence index |
| M5-01..05 | all verified；packet/checkpoint/canary/bounded/accounting receipts pass；provider metrics remain unavailable where unexported |
| M5-06..08 | all verified；replay `1000/1000`；Idea faults `6000/6000`；dogfood coverage/veto 100%；continuation fields/faults `10000/10000` |
| M8-04 trace | `1000/1000`；`8000` events；eight-family coverage 100%；OTel unavailable `1000/1000` |
| M8-01 durability | verified；focused `78/78`；Typed State v5/State MCP v2 与 v4↔v5 migration/rollback 门通过；9 crash points `180/180` recovered；semantic effects `180`，adapter calls `200`，deduplications `20`，duplicates `0`；restore p95 `105.039451 ms`；DeepSeek `2/2` real `SIGKILL` recovery |
| M8-02 coordination | verified；focused `81/81`；10 类场景 `10000/10000`；orphan reclaim `1000/1000`；八项零损失指标均为 `0`；p95 `0.528895 ms`；SQLite canonical snapshot/Event/revision/receipt 同事务；schema v3->v4 与 upgrade->rollback->upgrade 通过 |
| M8-03 workflow | verified local replay / optional Temporal adapter；`43` pass、`1` optional SDK skip；12 strict schemas；三代链 `1000/1000`、`3000` runs、`2000` rollovers、`7000/7000` faults；veto 指标全 `0`；p95 `23.122890 ms`；`temporalio==1.31.0` API conformance 通过；live service/worker deployment conditional |
| M6 retrieval/recall | all verified；五类 route `1000/1000` replay iterations、`5000/5000` decisions；quality/provenance `1.0`；duplicate read bytes `-50%`；reference conformance memory `0.6 -> 0.9`；safety veto `0`；external calls `0` |
| M7 evidence/quality | M7-01..06 verified；false authority/admission/test omission `0`；affected-test wall time reduction `97.13%`；详见 Evidence index |
| Repository verification | full `1442` passed / `31` conditional skipped；focused M8-02 `81/81`；focused M8-03 `43` pass / `1` optional skip；compile、changed-scope lint 和 repository verifier 通过 |
| Governance authority | `MASTER.md` revision 61 |

## 恢复入口

1. 普通任务先读本文件。
2. 根据 task ID 只展开 `MASTER.md` 对应章节。
3. 聊天迁移先读 `docs/policies/conversation-ingestion.md`。
4. 组件选择和实验结论读 `docs/research/context-reliability-assessment-2026-08-09.md`。
5. 当前代码或官方证据与历史 memory 冲突时，以当前证据为准。
6. 需要历史完成证据时按 `docs/reports/verified-state.md` 的 task/evidence ref 有界展开。
7. 压缩、Skill 选择或 MASTER revision 后按 `context.dogfood-event/v1alpha1` 追加受控观察事件；仅在 schema/hash 变化或 validator 失败时重读完整 policy。
8. 文档更新、拆分和 supersedes 按 `docs/policies/documentation-lifecycle.md`，恢复时只展开当前任务对应的最小合同。
