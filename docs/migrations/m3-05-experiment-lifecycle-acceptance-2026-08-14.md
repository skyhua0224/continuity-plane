# M3-05 Experiment Lifecycle Acceptance

版本：3  
日期：2026-08-14  
状态：verified local-embedded experiment lifecycle contract

```yaml
document_id: context.m3-05-experiment-lifecycle-acceptance
document_revision: 3
change_type: evidence
authority_ref: verification-run://repository/current-head
supersedes: context.document://docs-migrations-m3-05-experiment-lifecycle-acceptance-2026-08-14/revision/2
affected_tasks: [M3-05, M3-06, M5-01, M8-02]
next_review: M3-07
```

## 范围

M3-05 为 Experiment Work 提供 append-only attempt ledger、trusted-time expiry gate、attempt-bound effect authorization 和 verifier-authorized promotion。Experiment 的 `return_point_work_id`、`exit_criteria`、`attempt_budget`、`expires_at`、`promotion_target_work_id` 与 `mainline_authority=false` 在第一次 attempt 后保持不可变。预算消耗使用独立 attempt 记录，不使用可重写计数器；相同 request 在服务重启后返回原 receipt。

`context.experiment.attempt`、`context.experiment.effect`、`context.experiment.promotion.propose` 和 `context.experiment.promotion.approve` 使用 State MCP authorization、expected revision/CAS、validator 和 hash-chained Event。普通 `context.state.commit` 不能写入 Experiment lifecycle collections。过期 Experiment、过期 Claim、预算耗尽、stale source/target revision、缺少 verified exit evidence、pending Experiment effect、伪造或缺少独立 verifier 的请求均保持 read-only 且不追加 Event。Typed-state import 与 Event replay 同时拒绝过期 lifecycle timestamps、时间回退和没有对应 experiment transition 的 ledger 变更。

Promotion 记录冻结 source/target revision、attempt、contract digest 和每项 exit criterion 的 evidence。批准记录必须保持 proposal lineage；Experiment 的发现仍然需要后续 canonical queue/promotion 消费，不自动激活主线 Work。

## 验证

| 门 | 结果 |
|---|---|
| Attempt ledger | 预算 `N` 接受 `N` 个 attempt；第 `N+1` 个被拒绝；并发 final slot 仅一个成功；attempt request 在新 State MCP service 进程中原 receipt 重放 |
| Expiry and lease | `now >= expires_at`、`now >= lease_expires_at`、trusted clock regression 和无效时间均拒绝 claim、attempt、effect、route activation 或 promotion；拒绝路径零 Event |
| Effect provenance | Experiment effect 必须绑定已持久化 attempt、同一 Claim、actor 和 contract digest；generic effect/preflight 不能绕过专用工具；最终预算 attempt 可授权其自身 effect |
| Promotion | proposer 必须绑定 attempt；每项 exit criterion 必须有 evidence；approval 需要独立 verifier、精确 source/target/project revision、无 pending Experiment effect；审批重试跨进程返回原 receipt |
| Append-only and lineage | attempt/promotion ledger 改写被 reducer 拒绝；attempt 后 Experiment contract drift 被拒绝；Event transition 必须包含对应 ledger change；approved promotion 必须保留 proposal lineage |
| Versioning | `context.typed-state/v3alpha1`、`context.state-event/v4alpha1`、`context.experiment-lifecycle/v1alpha1` 和 benchmark receipt strict schema 已登记并 hash-bound；v2 -> v3 migration 幂等；存在 lifecycle history 时 v3 -> v2 rollback 被拒绝 |
| Focused regression | M3-05 定向测试 `27/27` 通过；无外部服务 |
| Repository gate | full repository `860/860` 通过、`30` 个可选 PostgreSQL 测试跳过；repository verifier 与 Python compile 通过 |

## 量化结果

[`m3-05-experiment-lifecycle-results.json`](../../experiments/routing/m3-05-experiment-lifecycle-results.json) 使用 [`m3-05-experiment-lifecycle.yaml`](../../experiments/routing/m3-05-experiment-lifecycle.yaml) 运行 40 个完整 attempt -> proposal -> approval 样本。每个样本写入 3 个 Event、1 个 attempt 和 2 条 promotion 记录。

| 指标 | 结果 |
|---|---:|
| successful runs | `40/40` |
| external services | `0` |
| events per run | `3` |
| attempt records per run | `1` |
| promotion records per run | `2` |
| total lifecycle p50 | `8.084453 ms` |
| total lifecycle p95 | `9.107612 ms` |
| total lifecycle max | `11.551386 ms` |

Receipt provenance 固定 fixture、lifecycle gate、State MCP、Event reducer、typed-state validator 和 benchmark runner 的 SHA-256；receipt 通过 `context.experiment-lifecycle-benchmark/v1alpha1` strict schema 和独立来源复验。

## 回滚

空 lifecycle ledger 可由 `migrate_v2alpha1_to_v3alpha1` 迁移后通过 `rollback_v3alpha1_to_v2alpha1` 返回 v2；任何 attempt、attempt-bound effect 或 promotion 记录存在时，rollback fail-closed。历史 v1/v2 Event 继续按原 wire contract replay，新的 lifecycle Event 使用 v4。

## 边界

本叶覆盖 local-embedded SQLite、单次 CAS、attempt/promotion durable replay 和 Experiment 内部副作用门。Evidence 与 attempt 的逐条 assertion provenance 尚未在 M2 schema 中表达，保留给 M7。共享 lease reclaim、跨设备 claim、durable workflow、forge adapter 和 Docmost 审批投影保留给 M8/M9；本 benchmark 不宣称 provider live compaction、真实 token、跨平台性能或 PostgreSQL 性能。

## 复现

```text
.venv/bin/python tools/run_experiment_lifecycle_benchmark.py --samples 40 --observed-at 2026-08-14T08:35:00+08:00 --output experiments/routing/m3-05-experiment-lifecycle-results.json
.venv/bin/python -m unittest tests.test_m3_05_experiment_lifecycle -q
```
