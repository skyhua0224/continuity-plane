# M3-06 Idea Continuity Acceptance

版本：3  
日期：2026-08-14  
状态：verified local-embedded candidate Idea contract

```yaml
document_id: context.m3-06-idea-continuity-acceptance
document_revision: 3
change_type: evidence
authority_ref: verification-run://repository/m3-06-idea-continuity-v2
supersedes: context.document://docs-migrations-m3-06-idea-continuity-acceptance-2026-08-14/revision/2
affected_tasks: [M3-06, M3-07, M5-06, M5-08]
next_review: M3-07
```

## 范围

M3-06 提供 `context.idea.capture`。`capture-and-continue`、`park` 和 `propose-switch` 只能分别建立 `candidate`、`parked` 和 `proposed` Idea。请求绑定当前 active Work 作为 parent 与 return point，要求 opaque `rng_` source ref、State MCP authorization、active owned Claim、trusted time 和 expected revision/CAS。Idea envelope 保存 bounded summary、source ref、parent、return point、expiry 和 proposal target；原始聊天正文保持在受控 archive 或 artifact ref。

每次成功 capture 追加一个 `context.idea-event/v1alpha1` Event。它与通用 `context.state-event/v1alpha1` 至 `v4alpha1` 共用 hash-chained stream，但属于独立 schema family，不声明跨 family migration。普通 `context.state.commit` 对 Typed State v3 的 `ideas` collection fail-closed，不能绕过专用 capture gate。Event replay 重新验证 parent、return point、expiry、actor/Claim、project projection 与 Idea change，并拒绝任何修改 active Work、claim authority、scope、effect watermark、Decision、Constraint、Effect 或 Work 的伪造 Idea Event。Idea Event schema 只允许一个 strict Idea change 和零个或多个 strict Claim revision changes；State MCP request schema 与 runtime 同时固定 opaque `rng_` source ref。

`propose-switch` 仅保存 target proposal，不激活 target、不释放当前 Claim、不授权副作用、也不写 task route transition。实际切换仍要求 M3-03 route apply、checkpoint、M3-04 scope/claim gate 和 CAS。

## 验证

| 门 | 结果 |
|---|---|
| Candidate authority | capture 后 active Work、primary Work、Claim scope 和 effect watermark 保持不变；Idea 只获得 `candidate`、`parked` 或 `proposed` 状态。 |
| Request and time | 无 active owned Claim、parent/return point 不匹配、无效 proposal target、expiry `<=` trusted time、失效 CAS 或 authorization 拒绝均保持零 Event、零 revision 变化。 |
| Dedicated commit path | Typed State v3 的 generic commit 写入 `ideas` 被拒绝；重启、后续 Event 和 registry digest 变化后，相同 capture request 均重放相同 event-bound receipt。 |
| Event replay | 单一 hash chain 可回放 `state-event/v4` 后接 `idea-event/v1`；Idea reducer 拒绝 active execution authority、Idea expiry、额外 Idea 或 collection change 被篡改的 Event。 |
| Schema governance | `context.idea-capture-request/v1alpha1`、`context.idea-event/v1alpha1` 和 benchmark receipt 均为 strict schema，已在 registry 以 SHA-256 绑定；Idea Event 只允许 strict Idea/Claim changes；通用 State Event current wire 保持 v4。 |
| Focused regression | `tests.test_m3_06_idea_continuity` 为 `15/15`；无外部服务。 |

## 量化结果

[`m3-06-idea-continuity-results.json`](../../experiments/routing/m3-06-idea-continuity-results.json) 使用独立 SQLite StateStore 执行 40 个 capture-and-continue 样本。每个样本追加一个 Idea Event，并检查 active execution authority 未改变。

| 指标 | 结果 |
|---|---:|
| successful runs | `40/40` |
| external services | `0` |
| events per run | `1` |
| ideas per run | `1` |
| total p50 | `3.051447 ms` |
| total p95 | `4.198319 ms` |
| total max | `4.881152 ms` |

Receipt provenance 固定 Idea gate、State MCP、Event reducer、Typed State、SQLite StateStore、registry、三个 M3-06 schema 和 benchmark runner 的 SHA-256。每个 run 比较完整执行权威投影，包含 Work、Claim authority、Decision、Constraint、Evidence、Blocker、Effect、Experiment ledger 与除 revision/timestamp 外的 Project 字段；receipt 通过 `context.idea-continuity-benchmark/v1alpha1` strict schema 及独立来源复验。

## 迁移与回滚

`context.idea-event/v1alpha1` 是 Idea-only envelope；`task_transition` 与 `experiment_transition` 必须为 null。通用 `context.state-event` current wire 保持 `v4alpha1`，两类 Event 通过同一 sequence、revision 和 previous hash 规则 replay。

回滚可关闭新的 Idea capture 写入并保留既有 Idea Event archive。删除 `idea_transition`、把 Idea Event 伪装成通用 State Event 或重写既有 hash chain 均被禁止；恢复读取器继续支持两个 schema family。

## 边界

M3-06 不实现 relationship、dedupe key、correction 写保护、urgency/impact review 或 packet exclusion；这些属于 M3-07 和 M5。它不宣称 provider live compaction、真实 token、跨设备协作、PostgreSQL 性能或自动 task switch 结果。

## 复现

```text
.venv/bin/python tools/run_idea_continuity_benchmark.py --samples 40 --observed-at 2026-08-14T08:45:00+08:00 --output experiments/routing/m3-06-idea-continuity-results.json
.venv/bin/python -m unittest tests.test_m3_06_idea_continuity -q
```
