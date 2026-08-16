# M3-08 Continuation Dispatch Acceptance

版本：4  
日期：2026-08-14  
状态：verified local-embedded continuation and bounded escalation contract

```yaml
document_id: context.m3-08-continuation-dispatch-acceptance
document_revision: 4
change_type: evidence
authority_ref: verification-run://repository/m3-08-continuation-dispatch-v4
supersedes: context.document://docs-migrations-m3-08-continuation-dispatch-acceptance-2026-08-14/revision/3
affected_tasks: [M3-08, M4-09, M5-01, M5-03, M5-08, M8-09]
next_review: M4-09
```

## 范围

M3-08 增加独立 continuation/dispatch 层。它消费 M3-02 的 canonical route decision，不复制输入分类合同；无活动叶时使用 `dispatch_tick` 计算下一项 required Work。所有决策为无副作用 proposal，State MCP 仍独占 revision/CAS、claim/lease、evidence 和外部副作用权限。

注册的 wire contracts：

- `context.continuation-dispatch-request/v1alpha1`
- `context.blocking-decision/v1alpha1`
- `context.continuation-dispatch-decision/v1alpha1`
- `context.continuation-dispatch-benchmark/v1alpha1`

`routed_input` 通过 route decision hash、project revision 和 active Work revision 绑定当前状态。`blocking_decision` 必须引用当前 open Blocker、verified Evidence、affected Work、owned scope、reason、resume condition 和 bounded resolution metadata。`dispatch_tick` 只选择 `work|experiment` 类型、typed/profile 双方 `ready`、required/pending/project-governance、依赖完成且无 blocker/protection 的 Work，按 `work_id` 升序确定 tie-break。

输入、Idea、状态查询、讨论和 route proposal 保留当前 active leaf。用户可解决的 blocker 生成 `ask-user`；仅依赖外部完成证据的 blocker 生成 `stop-blocked`；所有 ask/stop 均包含 reason、evidence、scope 和 resume condition。required closure 完成时生成 `stop-complete`；optional Work 不阻塞 closure。存在安全可逆默认时继续当前叶或选择可用 required Work。

## 验证

| 门 | 结果 |
|---|---|
| Non-blocking route | 9 类 M3-02 route decision 均保持 active Work、revision 和写权限；Idea 使用 `capture-and-continue` |
| Typed blocker binding | unknown、closed、stale、scope mismatch、未验证 evidence、缺 option/default/resume 均拒绝；safe reversible default 不升级 |
| Next-ready selector | required 优先于 optional；state/profile dependency 集一致；数组重排不改变选择；ready required 存在时 premature ask/stop 为 0 |
| Escalation taxonomy | 3 类 user blocker 只能 `ask-user`；external evidence blocker 只能 `stop-blocked`；无 typed blocker 的 pending required fail closed |
| Completion | 仅 `required` obligation 全部 `satisfied|waived` 且带 governance evidence 时 `stop-complete`；optional pending 不阻塞 |
| Wire/replay | 四份 strict schema 通过 Draft 2020-12；request、blocker、decision canonical bytes 可重放；state/profile/request 均保持 immutable |
| Repository gate | M3-08 定向行为与 benchmark `14/14` 通过；ruff、compileall、repository verifier、`git diff --check` 通过 |

## 量化结果

[`m3-08-input-progression-results.json`](../../experiments/routing/m3-08-input-progression-results.json) 在本地 embedded profile 运行 1,000 个有效样本，并额外执行 125 个无 blocker 负向夹具。每个有效样本执行两次 decision replay。

| 指标 | 结果 |
|---|---:|
| successful samples | `1000/1000` |
| negative fixture rejection | `125/125` |
| ready required missed | `0` |
| premature stop | `0` |
| untyped ask/stop | `0` |
| incomplete escalation | `0` |
| active leaf changes | `0` |
| nondeterministic replay | `0` |
| request/state/profile mutation | `0/0/0` |
| external services | `0` |
| decision p50 | `0.132634 ms` |
| decision p95 | `0.230881 ms` |
| decision max | `0.285398 ms` |

Receipt 通过 `context.continuation-dispatch-benchmark/v1alpha1` strict schema；provenance 固定 continuation implementation、M3-02 router、Typed State、Project Governance Profile、四份 schema 和 registry 的 SHA-256。validator 会在验收时重新计算这些 hash，过期 receipt 拒绝。

## 回滚与边界

可以停止 M3-08 dispatch proposal 生成，并继续读取已记录的 M3-02 route decision、M3-06/M3-07 Idea Event 和 Typed State。M3-08 不激活 target Work、不提交 claim、不写入 Blocker、不执行 stop transition，也不替代 M3-03 route apply、M5 Execution Packet 或 M8 unattended dispatcher。pending required 且无 ready leaf、无 typed blocker 的状态保持 fail closed，等待 State MCP 创建有证据的 blocker。

## 复现

```text
.venv/bin/python tools/run_input_progression_benchmark.py --samples 1000 --observed-at 2026-08-14T16:00:00+08:00 --output experiments/routing/m3-08-input-progression-results.json
.venv/bin/python -m unittest tests.test_m3_08_input_progression tests.test_m3_08_input_progression_benchmark -q
ruff check context_control_plane/input_progression.py context_control_plane/input_progression_benchmark.py tests/test_m3_08_input_progression.py tests/test_m3_08_input_progression_benchmark.py tools/run_input_progression_benchmark.py
.venv/bin/python tools/verify_repository.py --root .
```
