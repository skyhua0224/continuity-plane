# Idea Intake and Context Return Architecture

版本：2  
日期：2026-08-09  
状态：architecture contract / M3-06 local-embedded implementation

## 目标

用户可以在持续工作中自然补充想法、纠正、约束、问题和新方向。控制面必须保存其价值，同时保持 active task、执行权限和 return point 稳定。用户无需记忆命令或 Idea ID；provider adapter 将自然语言事件分类并提交候选，确定性状态门决定是否继续、暂停或切换。

## 输入分类

| 输入类型 | 默认处理 | 对 active task 的影响 |
|---|---|---|
| `status_query` | 返回当前状态后继续 | 无 |
| `discussion_request` | 回答当前问题并保留 continuation cursor | 无；仅产生 blocking decision 时升级 |
| `context_addition` | 绑定当前任务的 evidence/constraint candidate | 验证前无权威变化 |
| `correction` | 建立 supersedes proposal；相关写权限暂时降级到只读 | validator 通过后更新 revision |
| `idea` | capture、deduplicate、park | 无 |
| `child_work` | 建立 child proposal 与 return point | 获批前无 |
| `interrupt` | 先 checkpoint，再提交 switch proposal | 仅显式授权或 CAS 激活后切换 |
| `blocking_decision` | 保存 checkpoint 并发出带选项、证据和 resume condition 的询问 | 回答前冻结受影响副作用，其他独立 ready work 可继续 |
| `stop_or_replace` | 冻结副作用并保存 checkpoint | 按用户最新指令改变路由 |

Classifier 只输出候选类型、置信度和 evidence request。non-blocking input 无权改变 active leaf；低置信度本身不构成停止理由，router 先采用不改变权限、主线和不可逆副作用的保守路径。权限变化、不可逆外部副作用和主线 promotion 必须由确定性规则或用户确认裁决。

## Idea 合同

```yaml
idea_id: stable-id
parent_task_id: stable-id
source_thread_ref: opaque-hash
source_range_ref: opaque-range | null
captured_at: rfc3339
summary: string
scope: string
relationship: same-leaf | child | cross-cutting | unrelated
urgency: now | next | later | review-date
status: candidate | parked | proposed | approved | rejected | superseded | expired
dedupe_key: stable-hash
return_point: checkpoint-id
decision_deadline: rfc3339 | null
expiry: rfc3339 | null
attempt_budget: uint32 | null
promotion_target: work-id | null
evidence_refs: [artifact-ref | assertion-id]
```

Idea 正文留在受控 source range 或 content-addressed artifact；Typed State 保存最小 envelope。相同 `dedupe_key + parent_task_id + scope` 的重复输入合并为追加证据，不创建并行执行权限。涉及当前安全、事实错误或不可违反约束的 correction 优先于普通 Idea，并触发写权限保护。

## M3-06 实现边界

`context.idea.capture` 接受 `capture-and-continue`、`park` 和 `propose-switch` 三种受控请求。请求必须绑定当前 active Work 作为 parent 与 return point、提供 opaque `rng_` source range、通过 trusted time、active claim、authorization 和 expected revision/CAS 校验。Idea 仅保存 bounded summary、source ref、parent、return point、expiry 和 proposal target；正文不进入 Typed State。

capture 生成独立的 `context.idea-event/v1alpha1` Idea transition，并与 `context.state-event/v1alpha1` 至 `v4alpha1` 共用 append-only stream。reducer 验证 parent/return point、source ref、expiry、actor/claim 与 project projection，并拒绝任何改变 active work、claim owner、scope、effect watermark、Decision、Constraint、Effect 或 Work 的 Idea Event。`context.state.commit` 在 Typed State v3 中拒绝 Idea 写入，防止绕过专用 gate。

`capture-and-continue` 写入 `candidate`，`park` 写入 `parked`，`propose-switch` 写入 `proposed` 与 target ref。三种动作均不激活 target、不释放当前 claim、不授权副作用，也不写 route transition。后续 task switch 必须经 M3-03 route apply、checkpoint、scope/claim gate 和 CAS。M3-06 未实现 dedupe、relationship、correction、urgency/impact review 或 Execution Packet exclusion；这些合同属于 M3-07 与 M5。

## 默认路由

默认动作是 `capture-and-continue`：

1. 记录 Idea candidate 和单行摘要；
2. 绑定当前 active task、source ref 和 return point；
3. 返回简短捕获回执；
4. 保持 active leaf、claim、path owner 和唯一 next action；
5. 完整 Idea 仅在 triage、impact review 或用户要求时展开。

用户表达“现在做”“先暂停原任务”或同等明确意图时，router 执行受控切换：冻结新副作用、提交滚动 checkpoint、写入 `task_suspended`、校验目标 scope/claim/path owner、CAS 激活目标任务。用户表达“先记着”“之后评估”时，Idea 保持 parked。普通状态询问和旁注不得重置 active leaf。

## Bounded Escalation

bounded escalation 只在以下 typed blocker 成立时暂停受影响 Work：缺少执行不可逆外部副作用的授权；多个已批准约束相互冲突且不存在保守可逆路径；缺失选择会实质改变验收产物；完成门依赖当前不可取得的外部证据。询问必须包含 blocker ID、已验证证据、可选决策、默认行为、受影响 scope 和恢复条件。

实现、验证或研究存在安全的可逆默认值时，Agent 记录假设并继续。Idea、状态问题、解释请求、普通偏好、非承重措辞和与当前叶无关的参考发现保持 capture-and-continue。分析过程不能自行产生 task switch、promotion、完成声明或用户询问。

## 压缩与回返

PreCompact checkpoint 至少保存：

- canonical MASTER digest 与 active task revision；
- active leaf、claim、owner、path ownership 和唯一 next action；
- latest、reverted、rejected 和 superseded decision IDs；
- hard blocker、验证缺口、已执行 effect IDs 和禁止副作用；
- return point、相关 Idea IDs 和 `idea_queue_digest`；
- `skill_set_digest`、Execution Packet hash 和 evidence validity watermark。

Execution Packet 只包含与当前 active leaf 直接相关的 Idea ID 和单行摘要。Parked、cross-cutting 和 unrelated Idea 不进入 prompt、Skill resolver 或工具权限。PostCompact canary 必须先恢复原 active task、return point、禁止副作用和相关 Idea refs；失败时保持只读，并禁止新 task dispatch、代码写入、提交和部署。

切回原任务时，composer 从 checkpoint 生成 context return packet。该 packet 不复述整个聊天，只包含当前 revision、完成进度、最新决定、阻塞、验证证据、受影响文件、唯一 next action 和按需展开引用。当前源码或官方证据与 Idea 产生时的假设冲突时，Idea 标记 stale 或 superseded。

## 无 State MCP 过渡模式

State MCP profile 不可用时，`STATUS.md` 只记录 active work、blocker、next action 和已晋升到计划的 Idea。MASTER 只保存通过治理的长期任务；未晋升 Idea 不进入长期计划。历史聊天和 handoff 保持 background candidate，当前仓库证据决定恢复结果。

## 验收门

- 普通 Idea 捕获率 100%，active task revision 和副作用权限变化为 0；
- correction 在 current-evidence verification 前阻止相关写入，旧决定复活为 0；
- 未获批 child/interrupt/promotion 激活率为 0；
- non-blocking input 导致的 task switch、premature stop 和用户询问均为 0；
- blocking decision 的 reason、evidence、scope 和 resume condition 完整率为 100%；
- task switch、compaction、model/provider change 和进程恢复后的 return point 为 100%；
- 重复 Idea 合并结果确定，过期 Idea 不进入 Execution Packet；
- context return packet 恢复关键字段 100%，完整 Idea 正文复制率为 0；
- 256/512 等容量不足 canary 保持 veto，token 降幅不能覆盖恢复失败。
