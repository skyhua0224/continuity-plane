# M3-07 Idea Review Acceptance

版本：4  
日期：2026-08-14  
状态：verified local-embedded Idea review and correction contract

```yaml
document_id: context.m3-07-idea-review-acceptance
document_revision: 4
change_type: evidence
authority_ref: verification-run://repository/m3-07-idea-review-v4
supersedes: context.document://docs-migrations-m3-07-idea-review-acceptance-2026-08-14/revision/3
affected_tasks: [M3-07, M3-08, M5-03, M5-06, M6-02, M8-02]
next_review: M3-08
```

## 范围

M3-07 将 Idea candidate contract 升级为 `context.typed-state/v4alpha1`。v3 -> v4 通过 SQLite 原子 migration receipt 固定 source revision、event head、source/target snapshot hash 与 schema transition；迁移失败时 snapshot 和 receipt 一并回滚。

Idea v2 capture 对 summary、scope 和 opaque source ref 计算稳定 `dedupe_key`。并发 capture、stale CAS 和服务重启后的相同 key 请求均收敛到一个 canonical Idea，并追加 immutable occurrence；occurrence 保存原始 submitted Idea、source ref、actor、request hash 和 observed time。relationship 只能连接已存在 Idea，拒绝 unknown/self/duplicate/cycle。

`context.idea.review` 只更新 review、urgency、impact 和候选状态。review、protection、release 使用独立的 `context.idea-event/v2alpha1` transition；每个 Event 只能携带其 operation 的精确 delta。`parked`、`expired`、`rejected` 和 `superseded` Idea 被排除于 packet，terminal Idea 不能被重新打开。

correction protection 记录 affected Work、parent/dependency 引用和 canonical scope。active protection 拦截相交的 generic commit、route correction、effect preflight 和 effect authorization；pending Effect 只允许完成回执。release 是独立权限动作，要求当前 Typed State 中每个 evidence ID 都是 `validity=verified` 且有 `verified_at`，并保持 opening provenance。项目级 expected revision/CAS 防止并发双 release；保护解除后受影响写入恢复。

Idea 操作不改变 active Work、Claim、path ownership 或 effect watermark。原始聊天正文不写入 Typed State；只保存 bounded summary、opaque provenance 和 evidence refs。

## 验证

| 门 | 结果 |
|---|---|
| Typed State migration | v3 -> v4 receipt 是原子、幂等、hash-bound；fault injection 不留下半迁移 snapshot 或 receipt |
| Dedupe and occurrence | Unicode case/whitespace canonicalization 固定 dedupe key；并发与 stale CAS 只产生一个 canonical Idea；所有 occurrence 保留 |
| Relationship | unknown/self/duplicate/cycle relationship 全部在 Event 前拒绝 |
| Review and packet | review 的 urgency/impact/deadline 受 schema 与 trusted time 约束；parked、expired、rejected、superseded Idea packet eligibility 为 0 |
| Correction protection | 受影响 Work、parent/dependency 和 canonical scope 的新写入均被拒绝；pending Effect completion receipt 可提交；generic commit、route apply 和 effect gate 无绕过路径 |
| Verified release | 缺失、未验证或复用 evidence 的 release 为 0；并发 release 只有一个 CAS winner，loser 返回显式 conflict |
| Replay and identity | v1 history 与 v2 Event 可从持久 migration boundary replay；同 request 同 payload 重放原 receipt，异 payload 返回 conflict；forged release/guard/review delta 被拒绝 |
| Focused regression | M3-07 定向行为与 benchmark `40/40` 通过；M3-06、M2-05、M2-09、M3-03 联合回归 `114/114` 通过；无 DSN 的 PostgreSQL parity `1` 项跳过 |
| Repository gate | schema governance、repository verifier、compileall、`git diff --check` 通过；新增 M3-07 Python 文件 Ruff 通过 |

## 量化结果

[`m3-07-idea-review-results.json`](../../experiments/routing/m3-07-idea-review-results.json) 在独立 SQLite StateStore 上运行 40 个样本，每个样本执行 capture、dedupe occurrence、review、correction protection，并验证拒绝与终态边界。

| 指标 | 结果 |
|---|---:|
| successful runs | `40/40` |
| external services | `0` |
| events per run | `4` |
| dedupe convergence | `100%` |
| occurrence retention | `100%` |
| packet exclusion | `100%` |
| unauthorized protected writes | `0` |
| terminal revival | `0` |
| unverified release | `0` |
| total p50 | `13.466169 ms` |
| total p95 | `15.362956 ms` |
| total max | `20.927312 ms` |

Receipt 通过 `context.idea-review-benchmark/v1alpha1` strict schema；provenance 固定 Idea review、State MCP、Event reducer、route/effect gates、Typed State、SQLite、registry、v4 与 v2 schema 以及 benchmark runner 的 SHA-256。每个样本比较完整 execution authority fingerprint，验证 Idea 操作没有改变 active Work、Claim、Decision、Constraint、Evidence、Blocker、Effect、Experiment ledger 或 canonical scope。

## 回滚与边界

可以停止新的 Idea v2 capture/review/protection 写入，并继续读取 v1 history 和已持久 v2 Event。删除 occurrence、重写 dedupe key、把 release 伪装成 guard 或通用 State Event、绕过 migration receipt 均被禁止。M3-07 不宣称 provider live compaction、真实 token 下降、跨设备共享 lease、PostgreSQL 性能、M5 Execution Packet 或 M6 relationship enrichment 结果。

## 复现

```text
.venv/bin/python tools/run_idea_review_benchmark.py --samples 40 --observed-at 2026-08-14T10:00:00+08:00 --output experiments/routing/m3-07-idea-review-results.json
.venv/bin/python tools/run_idea_continuity_benchmark.py --samples 40 --observed-at 2026-08-14T08:45:00+08:00 --output experiments/routing/m3-06-idea-continuity-results.json
.venv/bin/python -m unittest tests.test_m3_07_idea_review tests.test_m3_07_idea_review_benchmark -q
.venv/bin/python -m unittest tests.test_m3_06_idea_continuity tests.test_m2_05_state_mcp tests.test_m2_09_sqlite_state_store tests.test_m2_09_sqlite_benchmark tests.test_m3_03_route_apply -q
.venv/bin/python tools/verify_repository.py --root .
```
