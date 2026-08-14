# Context Control Plane Status

版本：revision 52  
日期：2026-08-14  
canonical plan：`MASTER.md`

## 当前状态

| 字段 | 值 |
|---|---|
| 当前 Campaign | M5 压缩与 Context Composition |
| active work | M5-03：PostCompact deterministic canary 与 host compaction 对照（🟡） |
| next action | M5-03：为 restore canary 与 Pi/DeepSeek mismatch 写失败测试；写权限 0 |
| hard blocker | M3-01：待 M2-09 Windows/macOS 原生 live fixture |
| repository mode | research / shadow pilot |
| production state | local-embedded implemented / shadow pilot；shared production pilot planned |

## 已验证摘要

| 对象 | 状态 |
|---|---|
| Evidence index | [`docs/reports/verified-state.md`](docs/reports/verified-state.md) |
| Documentation lifecycle | M0-10 verified；High/Medium review 0；容量门通过 |
| Git admission | M0-11 offline contract `25/25`；repository verifier 通过 |
| Skill adapters | M4-07/08/10 done；external=0 |
| Project dogfood | compaction=27；Cursor=78/80；first/replay=2/2；provider metrics unavailable |
| M3-07 Idea review | `40/40`；dedupe/occurrence/packet=100%；protected/terminal/unverified=0；p95 `20.826929 ms` |
| M3-08 continuation | `1000/1000`；negative `125/125`；6 faults=0；p95 `0.26578 ms` |
| M4-09 Skill resolver | replay `1000/1000`；quarantine `125/125`；faults=0；reduction `33.3333%`；p95 `1.109641 ms`；external=0 |
| M5-01 Execution Packet | replay `1000/1000`；canary failures=0；authority=0；packet `4436 B`；p95 `0.517655 ms`；external=0 |
| M5-02 checkpoint | replay `1000/1000`；delta `1093 B`；p50/p95/max `0.639104/0.666804/1.111154 ms`；mismatch/authority `0`；external=0 |
| Repository verification | unittest `961`；30 PostgreSQL skips；verifier、compileall、diff check 通过 |
| Governance authority | `MASTER.md` revision 52 |

## 恢复入口

1. 普通任务先读本文件。
2. 根据 task ID 只展开 `MASTER.md` 对应章节。
3. 聊天迁移先读 `docs/policies/conversation-ingestion.md`。
4. 组件选择和实验结论读 `docs/research/context-reliability-assessment-2026-08-09.md`。
5. 当前代码或官方证据与历史 memory 冲突时，以当前证据为准。
6. 需要历史完成证据时按 `docs/reports/verified-state.md` 的 task/evidence ref 有界展开。
7. 压缩、Skill 选择或 MASTER revision 后按 `context.dogfood-observation/v1alpha1` 追加受控观察事件；仅在 schema/hash 变化或 validator 失败时重读完整 policy。
8. 文档更新、拆分和 supersedes 按 `docs/policies/documentation-lifecycle.md`，恢复时只展开当前任务对应的最小合同。

## 当前控制门

| 门 | 约束 |
|---|---|
| Raw archive access | 大型 rollout 采用流式、区间化读取 |
| Source namespace | key material 只能由 secret manager/受保护文件/进程注入；Git 和公开记录只保留安全 key ID |
| Historical memory | handoff、project memory 和聊天摘要保持 candidate 状态 |
| Idea intake | 默认 capture-and-continue；明确 interrupt 先 checkpoint；未批准 Idea 不改变 active state |
| Fixture admission | sanitizer、provenance、current-evidence verification 和 independent validator 全部通过后开放 |
| External Skill activation | 需固定 revision/hash、license/provenance、权限检查、审批和 replay；外部 active 0 |
| Reference adoption | 发现器只写 candidate/stale signal；承重 assertion 必须 current provenance 与 validator |
| Platform boundary | Context Control Plane 通过外部 Project Profile 与 API 集成 |
| Repository topology | modular、monolith、mixed 使用同一 core；Foundation Sunshine 当前按 monolith Profile 协作，模块边界不构成准入条件 |
| Runtime profile | 默认 `local-embedded` 无独立数据库/daemon；Git remote 只触发 `forge-coordinated` proposal；PostgreSQL、Temporal、Docmost、OTel 均为 opt-in |
