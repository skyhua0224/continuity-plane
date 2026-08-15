# Context Control Plane Status

版本：revision 57  
日期：2026-08-16  
canonical plan：`MASTER.md`

## 当前状态

| 字段 | 值 |
|---|---|
| 当前 Campaign | M7 幻觉、证据与代码质量 |
| active work | M7-03：项目化 Verification Profile（🟡） |
| next action | M7-03：定义 provider-neutral profile、required/conditional/optional gate 和项目 adapter 合同 |
| hard blocker | 当前叶无；M3-01 的 Windows/macOS 原生 live fixture 保持 conditional |
| repository mode | research / shadow pilot |
| production state | local-embedded implemented / shadow pilot；shared production pilot planned |

## 已验证摘要

| 对象 | 状态 |
|---|---|
| Evidence index | [`docs/reports/verified-state.md`](docs/reports/verified-state.md) |
| Documentation lifecycle | M0-10 verified；STATUS reduction `76.3850%`；MASTER section reduction `68.9812%`；High/Medium review 0 |
| Git admission | M0-11 offline contract `26/26`；repository verifier 通过 |
| Skill adapters | M4-07/08/10 done；external=0 |
| Prior gates | M3-07/M3-08/M4-09 verified；replay and quarantine gates pass；details in evidence index |
| M5-01..05 | all verified；packet/checkpoint/canary/bounded/accounting receipts pass；provider metrics remain unavailable where unexported |
| M5-06..08 | all verified；replay `1000/1000`；Idea faults `6000/6000`；dogfood coverage/veto 100%；continuation fields/faults `10000/10000` |
| M8-04 trace | `1000/1000`；`8000` events；eight-family coverage 100%；OTel unavailable `1000/1000` |
| M6 retrieval/recall | all verified；五类 route `1000/1000` replay iterations、`5000/5000` decisions；quality/provenance `1.0`；duplicate read bytes `-50%`；reference conformance memory `0.6 -> 0.9`；safety veto `0`；external calls `0` |
| M7-01 provenance | verified；bearing candidate/expiry/digest faults fail closed；committed coverage `1.0` |
| M7-02 claim-evidence | verified；`1000/1000` replay；750/750 负向样本拒绝；false allow/deny `0`；State/completion authority `0` |
| Repository verification | full `1175` passed / `30` optional-environment skipped；focused M7-01/M7-02/schema `40/40`；repository verifier passed |
| Governance authority | `MASTER.md` revision 57 |

## 恢复入口

1. 普通任务先读本文件。
2. 根据 task ID 只展开 `MASTER.md` 对应章节。
3. 聊天迁移先读 `docs/policies/conversation-ingestion.md`。
4. 组件选择和实验结论读 `docs/research/context-reliability-assessment-2026-08-09.md`。
5. 当前代码或官方证据与历史 memory 冲突时，以当前证据为准。
6. 需要历史完成证据时按 `docs/reports/verified-state.md` 的 task/evidence ref 有界展开。
7. 压缩、Skill 选择或 MASTER revision 后按 `context.dogfood-event/v1alpha1` 追加受控观察事件；仅在 schema/hash 变化或 validator 失败时重读完整 policy。
8. 文档更新、拆分和 supersedes 按 `docs/policies/documentation-lifecycle.md`，恢复时只展开当前任务对应的最小合同。

## 当前控制门

| 门 | 约束 |
|---|---|
| Data and memory | raw archive 流式读取；secret 只经受保护注入；memory/handoff 为 candidate |
| Routing and admission | Idea 默认 capture-and-continue；fixture 需 sanitizer、provenance、current-evidence 和 validator |
| Skills and evidence | 外部 Skill 固定 revision/hash/license/provenance 后 candidate-only；承重 assertion 需 current provenance |
| Authority boundary | MASTER 为治理权威；Typed State/State MCP 管 active state；trace、memory、文档投影无绕过写权 |
| Runtime and topology | modular/monolith/mixed 共用 core；默认 `local-embedded`；Git forge、PostgreSQL、Temporal、Docmost、OTel opt-in |
