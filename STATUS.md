# Context Control Plane Status

版本：revision 58  
日期：2026-08-16  
canonical plan：`MASTER.md`

## 当前状态

| 字段 | 值 |
|---|---|
| 当前 Campaign | M8 多协作者与耐久执行 |
| active work | M8-01：DBOS checkpoint/effect workflow 与 harness crash fixture（🟡） |
| next action | 定义 local-embedded durable operation、intent/effect/settlement crash matrix 和 DeepSeek/Pi harness 对照 fixture |
| hard blocker | 当前叶无；M3-01 的 Windows/macOS 原生 live fixture 保持 conditional |
| repository mode | research / shadow pilot |
| production state | local-embedded implemented / shadow pilot；shared production pilot planned |

## 已验证摘要

| 对象 | 状态 |
|---|---|
| Evidence index | [`docs/reports/verified-state.md`](docs/reports/verified-state.md) |
| Documentation lifecycle | M0-10 verified；STATUS reduction `76.4291%`；MASTER section reduction `68.9812%`；High/Medium review 0 |
| Git admission | M0-11 offline contract `26/26`；repository verifier 通过 |
| Skill adapters | M4-07/08/10 done；external=0 |
| Prior gates | M3-07/M3-08/M4-09 verified；replay and quarantine gates pass；details in evidence index |
| M5-01..05 | all verified；packet/checkpoint/canary/bounded/accounting receipts pass；provider metrics remain unavailable where unexported |
| M5-06..08 | all verified；replay `1000/1000`；Idea faults `6000/6000`；dogfood coverage/veto 100%；continuation fields/faults `10000/10000` |
| M8-04 trace | `1000/1000`；`8000` events；eight-family coverage 100%；OTel unavailable `1000/1000` |
| M6 retrieval/recall | all verified；五类 route `1000/1000` replay iterations、`5000/5000` decisions；quality/provenance `1.0`；duplicate read bytes `-50%`；reference conformance memory `0.6 -> 0.9`；safety veto `0`；external calls `0` |
| M7-01 provenance | verified；bearing candidate/expiry/digest faults fail closed；committed coverage `1.0` |
| M7-02 claim-evidence | verified；`1000/1000` replay；750/750 负向样本拒绝；false allow/deny `0`；State/completion authority `0` |
| M7-03 Verification Profile | verified；AlkaidLab 与 `portable-python-library`；12/12 focused；`1000/1000` replay；false allow/deny `0/0`；authority `0` |
| M7-04 patch A/B | verified local contract；25/25 focused；`1000/1000` replay；false admit/reject `0/0`；9 strict schemas；真实 provider A/B 保留 M10 |
| M7-05 affected tests | verified；21/21 focused；`1000/1000` replay；漏测/unsafe/fallback/replay mismatch `0`；本仓 wall time reduction `97.13%` |
| M7-06 ReferenceWatcher | verified offline；22/22 focused；`1000/1000` replay；change stale/quarantine `400/400`；adversarial reject `2600/2600`；unreviewed completion `0/800` |
| Repository verification | full `1175` passed / `30` optional-environment skipped；focused M7-01/M7-02/schema `40/40`；repository verifier passed |
| Governance authority | `MASTER.md` revision 58 |

## 恢复入口

1. 普通任务先读本文件。
2. 根据 task ID 只展开 `MASTER.md` 对应章节。
3. 聊天迁移先读 `docs/policies/conversation-ingestion.md`。
4. 组件选择和实验结论读 `docs/research/context-reliability-assessment-2026-08-09.md`。
5. 当前代码或官方证据与历史 memory 冲突时，以当前证据为准。
6. 需要历史完成证据时按 `docs/reports/verified-state.md` 的 task/evidence ref 有界展开。
7. 压缩、Skill 选择或 MASTER revision 后按 `context.dogfood-event/v1alpha1` 追加受控观察事件；仅在 schema/hash 变化或 validator 失败时重读完整 policy。
8. 文档更新、拆分和 supersedes 按 `docs/policies/documentation-lifecycle.md`，恢复时只展开当前任务对应的最小合同。
