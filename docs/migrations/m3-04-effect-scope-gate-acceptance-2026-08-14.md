# M3-04 Effect Scope Gate Acceptance

版本：2  
日期：2026-08-14  
状态：verified local-embedded effect authorization contract

```yaml
document_id: context.m3-04-effect-scope-gate-acceptance
document_revision: 2
change_type: evidence
authority_ref: verification-run://repository/current-head
supersedes: context.document://docs-migrations-m3-04-effect-scope-gate-acceptance-2026-08-14/revision/1
affected_tasks: [M3-04, M4-09, M8-02]
next_review: M3-05
```

## 范围

M3-04 定义 `context.effect-scope-verdict/v1alpha1` 与确定性 scope resolver。Claim 申请、Effect authorize、Effect complete 和 route switch/interrupt 在状态提交前验证 active Work、actor ownership、Claim revision、scope ownership、pending Effect 与 expected revision。Effect authorize 使用可信时钟验证 lease；Effect complete 绑定原 Claim actor 与已授权 Effect provenance，不重新授予执行权限。超期 Claim 的已授权 Effect 完成时，在同一 Event 写入 Effect receipt、`expired` Claim 和 `verifying` Work。拒绝路径保持 read-only，不追加 Event，不推进 project revision。

v2 path scope 使用 `repo://<repository-id>/...` canonical form。`repo` 覆盖同 repository 下的 directory、file 和 symbol；directory 覆盖自身与后代；file 覆盖自身与其 symbol；symbol、capability 和 effect 使用精确匹配。相对路径、绝对路径、反斜杠、`..`、URI alias 与空 symbol fragment被拒绝。v1 历史 snapshot 保持 legacy exact-scope replay 语义。v2 Typed State replay 使用 hierarchy resolver，验证活跃 Claim hierarchy overlap 与 pending Effect hierarchy overlap，并保留既有 Effect operation/scope provenance 的可回放性。

State MCP request/response v1 保持 strict error envelope，并公开 `context.state.effect.gate` read-only preflight。preflight 返回 versioned verdict；`context.state.effect` 在提交前重算 gate。write operation 只接受 path scope，Git 和 correction operation 只接受 capability scope，deploy/external operation 只接受 effect scope。route switch/interrupt 在释放 source Claim 前拒绝 source Work 的 authorized/started Effect，并用同一 claim gate 校验 target actor、scope 和 overlap。

## 验证

| 门 | 结果 |
|---|---|
| scope hierarchy | v2 `repo://` repo/directory/file/symbol 覆盖与 capability/effect 精确匹配；prefix collision、path escape、absolute path、relative path、backslash 与 fragment drift 被拒绝；v1 exact replay 保持可读 |
| Claim ownership | Work declared scope 覆盖 Claim scope；第二个 Work 对层级重叠 scope 的 Claim 在 Event 前返回 conflict/read-only |
| Effect ownership | authorize 验证 active Work、同 actor Claim、Claim revision、lease、scope、operation class 与 expected revision；complete 验证原 Claim actor、pending Effect provenance 与 expected revision；超期已授权 Effect 在 Effect/Claim/Work 同一 Event 收据化；失配路径返回 deny/read-only 且零 Event |
| Pending Effect | 重叠 authorized/started Effect 在 Event 前返回 conflict/read-only；route switch/interrupt 不能释放带 pending Effect 的 source Claim；Typed State replay 同步拒绝 |
| Wire contract | `context.effect-scope-verdict/v1alpha1` strict schema 与 State MCP five-tool request/result/response contract 已登记；allow/deny、reason 与 read-only 条件一致；registry hash 与 artifact 一致 |
| Focused regression | M3-04 gate/benchmark、M2-05 State MCP、M3-03 route apply 共 71/71 通过；PostgreSQL parity 1 个无 DSN skip；M2/M3 全量复验在本叶提交门完成，另有无 DSN PostgreSQL skips |
| Repository gate | full repository `832/832` 通过、`30` 个可选 PostgreSQL 测试跳过；Python compile、`git diff --check`、repository verifier 通过 |

## 量化结果

[`m3-04-effect-scope-verification.json`](../../experiments/routing/m3-04-effect-scope-verification.json) 记录 baseline 10,000 个本地 gate evaluation：5,000 allow、5,000 out-of-scope read-only deny、state mutation 0。Linux x86_64 / CPython 3.14.6 baseline p50 为 `0.005548 ms`，p95 为 `0.005879 ms`。pending-effect conflict load 各 1,000 个样本：cardinality `1/100/1000` 的 p95 为 `0.008656/0.537702/10.067423 ms`，每组 `1,000/1,000` read-only deny；外部服务为 0。

## 边界

本叶提供单进程 local-embedded 层级 ownership、超期完成收据恢复和副作用前判定。共享 heartbeat、lease reclaim、跨设备并发 lease、Git forge path 事实同步、远程 effect provider、tenant isolation 和 durable retry 保留给 M8。PostgreSQL live State MCP parity 已有 M2-05 receipt；M3-04 的 PostgreSQL-specific performance 与跨进程 lease receipt 不在本叶范围。

## 复现

```text
.venv/bin/python tools/run_effect_scope_benchmark.py --samples 10000 --conflict-samples 1000 --pending-effect-counts 1,100,1000 --observed-at 2026-08-14T08:00:00+08:00 --output experiments/routing/m3-04-effect-scope-verification.json
.venv/bin/python -m unittest tests.test_m3_04_effect_scope_gate tests.test_m3_04_effect_scope_benchmark tests.test_m2_05_state_mcp tests.test_m3_03_route_apply -v
.venv/bin/python -m unittest discover -s tests -p 'test_m2_*.py' -q
.venv/bin/python -m unittest discover -s tests -p 'test_m3_*.py' -q
```
